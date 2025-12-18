#!/usr/bin/env python3

import argparse
import configparser
import logging
import os
import subprocess
import sys
import re
import tempfile
import requests
import socket
from typing import List, Optional, Set

# Third-party libraries, assuming they will be installed via requirements.txt
try:
    from plexapi.server import PlexServer
    from plexapi.video import Video
    from plexapi.myplex import MyPlexAccount
    from tqdm import tqdm
except ImportError:
    print("Required libraries are not installed. Please run 'pip install -r requirements.txt'")
    sys.exit(1)

from filename_cleaner import clean_filename


def send_failure_email(
    to_addr: Optional[str],
    from_addr: Optional[str],
    video_title: str,
    source_path: str,
    error_message: str
):
    """
    Sends an email notification about a transcoding failure using sendmail.
    """
    if not to_addr or not from_addr:
        logging.info("Mail 'to' or 'from' address not configured. Skipping email notification.")
        return

    try:
        subject = f"Transcode Failure: {video_title}"
        hostname = socket.gethostname()
        
        # Construct email body with headers
        body = (
            f"From: {from_addr}\n"
            f"To: {to_addr}\n"
            f"Subject: {subject}\n\n"
            f"A critical error occurred while transcoding a file on {hostname}.\n\n"
            f"Video Title: {video_title}\n"
            f"Source File: {source_path}\n\n"
            f"Error Details:\n"
            f"--------------------------------------\n"
            f"{error_message}\n"
            f"--------------------------------------\n"
        )
        
        # The -t flag tells sendmail to read recipients from the message headers
        process = subprocess.Popen(
            ['/usr/sbin/sendmail', '-t'], 
            stdin=subprocess.PIPE, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        stdout, stderr = process.communicate(body)
        
        if process.returncode == 0:
            logging.info(f"Successfully sent failure notification email to {to_addr}")
        else:
            logging.error(
                f"Failed to send failure email via sendmail. Exit code: {process.returncode}\n"
                f"  - Stderr: {stderr.strip()}\n"
                f"  - Stdout: {stdout.strip()}"
            )
    except FileNotFoundError:
        logging.error("sendmail command not found at /usr/sbin/sendmail. Cannot send email notification.")
    except Exception as e:
        logging.error(f"An unexpected error occurred while sending email: {e}", exc_info=True)


def get_plex_videos(plex: PlexServer, reverse_sort: bool = False) -> List[Video]:
    """
    Connects to the Plex server and compiles a list of all unique video objects
    from the "Van" playlists of all users.
    """
    try:
        logging.info("Fetching users from MyPlexAccount...")
        account = plex.myPlexAccount()
        users = account.users()
        logging.info(f"Found {len(users)} users.")
    except Exception as e:
        logging.error(f"Could not fetch Plex users. Please check your token and network connection. Error: {e}")
        # Return empty list if users can't be fetched
        return []

    all_videos: Set[Video] = set()
    
    # Add owner's playlists
    try:
        for playlist in plex.playlists():
            if playlist.title.strip().lower() == 'van':
                logging.info(f"Found 'Van' playlist for server owner.")
                for item in playlist.items():
                    all_videos.add(item)
    except Exception as e:
        logging.error(f"Could not fetch playlists for the server owner. Error: {e}")


    # Add shared users' playlists
    for user in users:
        try:
            user_plex = PlexServer(plex._baseurl, user.get_token(plex.machineIdentifier))
            for playlist in user_plex.playlists():
                if playlist.title.strip().lower() == 'van':
                    logging.info(f"Found 'Van' playlist for user: {user.title}")
                    for item in playlist.items():
                        all_videos.add(item)
        except Exception as e:
            logging.warning(f"Could not process playlists for user '{user.title}'. They may not have accepted the library share. Error: {e}")
            continue
    # Sort the unique videos by their file path for deterministic processing
    sorted_videos = sorted(list(all_videos), key=lambda video: video.media[0].parts[0].file, reverse=reverse_sort)
            
    return sorted_videos

def find_english_subtitle_stream(video: Video, plex: PlexServer) -> Optional[str]:
    """
    Finds an English subtitle stream for a given video and returns its stream key.
    Plex's built-in subtitle burning is preferred.
    """
    for stream in video.subtitleStreams():
        if stream.languageCode == 'eng' and stream.codec:
            logging.info(f"Found English subtitle stream for '{video.title}'")
            # This returns a Plex-internal URL to the subtitle stream
            return stream.key
    logging.info(f"No English subtitle stream found for '{video.title}'")
    return None

def is_transcode_valid(source_path: str, dest_path: str, duration_tolerance_percent: float = 1.0) -> bool:
    """
    Checks if a transcoded file is valid by comparing its duration to the source file's duration.
    A file is considered valid if its duration is within a certain percentage tolerance of the source.
    Requires ffprobe to be in the system's PATH.
    """
    if not os.path.exists(dest_path):
        return False

    def get_duration(file_path: str) -> Optional[float]:
        """Helper to get media duration using ffprobe."""
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return float(result.stdout.strip())
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as e:
            logging.error(f"Failed to get duration for '{file_path}'. Error: {e}")
            # If ffprobe isn't found or fails, assume invalid to trigger re-transcode
            return None

    source_duration = get_duration(source_path)
    dest_duration = get_duration(dest_path)

    if source_duration is None or dest_duration is None:
        logging.warning(f"Could not validate duration for '{os.path.basename(dest_path)}', assuming it's invalid.")
        return False

    # Avoid division by zero for very short/empty files
    if source_duration == 0:
        if dest_duration == 0:
            return True
        else:
            logging.warning(f"Validation failed for '{os.path.basename(dest_path)}'. Source duration is 0 but destination is {dest_duration:.2f}s.")
            return False

    # Calculate percentage difference
    duration_diff_percent = abs(source_duration - dest_duration) / source_duration * 100

    if duration_diff_percent <= duration_tolerance_percent:
        return True
    else:
        logging.warning(
            f"Validation failed for '{os.path.basename(dest_path)}'. "
            f"Duration difference is {duration_diff_percent:.2f}%, which is over the {duration_tolerance_percent}% threshold.\n"
            f"  - Source duration: {source_duration:.2f}s\n"
            f"  - Destination duration: {dest_duration:.2f}s"
        )
        return False

def transcode_video(
    video: Video,
    plex: PlexServer,
    config: dict,
    dry_run: bool = False,
    use_qsv: bool = False
):
    """
    Manages the transcoding of a single video file.
    """
    media_dir = config['media_dir']
    local_media_dir = config.get('local_media_dir') or config['media_dir']
    output_dir = config['output_dir']
    mail_to = config.get('mail_to')
    mail_from = config.get('mail_from')

    temp_sub_path = None
    try:
        # This is the path as Plex sees it
        plex_source_path = video.media[0].parts[0].file
        
        # This is the path on the local machine running the script
        relative_path = os.path.relpath(plex_source_path, media_dir)
        local_source_path = os.path.join(local_media_dir, relative_path)

        if not os.path.exists(local_source_path):
            logging.error(f"Source file not found at local path: '{local_source_path}'. Skipping.")
            return

        # 1. Construct destination path
        dest_dir = os.path.join(output_dir, os.path.dirname(relative_path))
        
        # Get the original filename without extension
        base_filename = os.path.splitext(os.path.basename(relative_path))[0]
        
        # Clean the filename using the utility function
        cleaned_filename = clean_filename(base_filename)

        # Ensure the filename is not empty after cleaning
        if not cleaned_filename:
            # Fallback to a simple filename if everything was stripped
            cleaned_filename = "transcoded_file"
            logging.warning(f"Cleaned filename for '{base_filename}' resulted in empty string. Using '{cleaned_filename}' as fallback.")
        
        # Set the new extension and join parts to form the final path
        destination_path = os.path.join(dest_dir, cleaned_filename + '.mkv')

        # 2. Check if a valid transcode already exists using the hybrid approach
        if os.path.exists(destination_path):
            source_is_newer = os.path.getmtime(local_source_path) > os.path.getmtime(destination_path)
            if not source_is_newer and is_transcode_valid(local_source_path, destination_path):
                logging.info(f"SKIPPING: Valid and up-to-date file exists: '{destination_path}'")
                return
            
            if source_is_newer:
                logging.info(f"Re-transcoding: Source file is newer ('{os.path.basename(local_source_path)}').")
            else: # is_transcode_valid must have been false
                logging.info(f"Re-transcoding: Existing file failed validation ('{os.path.basename(destination_path)}').")

        # 3. Create destination directory
        os.makedirs(dest_dir, exist_ok=True)


        # 4. Construct ffmpeg command
        ffmpeg_cmd = [
            'ffmpeg',
            '-i', local_source_path
        ]

        # Video filters
        video_filters = "scale=w=1280:h=720:force_original_aspect_ratio=decrease,pad=w=1280:h=720:x=(ow-iw)/2:y=(oh-ih)/2"
        
        # Subtitle filters
        subtitle_key = find_english_subtitle_stream(video, plex)
        if subtitle_key:
            try:
                subtitle_url = plex.url(subtitle_key) + f"?X-Plex-Token={plex._token}"
                logging.info(f"Downloading subtitle from {subtitle_url}")
                response = requests.get(subtitle_url, stream=True, timeout=20)
                response.raise_for_status()

                # Create a temporary file to store the subtitle
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.srt', delete=False) as temp_sub_file:
                    temp_sub_path = temp_sub_file.name
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_sub_file.write(chunk)
                
                logging.info(f"Subtitle downloaded to temporary file: {temp_sub_path}")
                
                # ffmpeg on Windows requires path escaping, this is safer for all platforms
                escaped_path = temp_sub_path.replace('\\', '\\\\').replace(':', '\\:')
                video_filters = f"subtitles='{escaped_path}',{video_filters}"

            except requests.exceptions.RequestException as e:
                logging.error(f"Failed to download subtitle file: {e}. Transcoding without subtitles.")
                temp_sub_path = None # Ensure it's not processed in finally block
        
        ffmpeg_cmd.extend(['-vf', video_filters])

        # Audio filters
        audio_filters = "acompressor=threshold=0.1:ratio=2:attack=20:release=200"
        ffmpeg_cmd.extend(['-ac', '2', '-af', audio_filters])

        # Video codec settings
        if use_qsv:
            logging.info("Attempting to use Intel QSV for HEVC transcoding.")
            video_codec_settings = [
                '-c:v', 'hevc_qsv',
                '-preset', 'medium',
                '-global_quality', '28'
            ]
        else:
            logging.info("Using libx265 for HEVC software transcoding.")
            video_codec_settings = [
                '-c:v', 'libx265',
                '-preset', 'medium',
                '-crf', '28'
            ]
        
        ffmpeg_cmd.extend(video_codec_settings)

        # Common output settings
        ffmpeg_cmd.extend([
            '-c:a', 'aac',
            '-b:a', '192k',
            '-y', # Overwrite output file if it exists (safety, though we check above)
            destination_path
        ])
        
        logging.info(f"Transcoding '{local_source_path}' to '{destination_path}'")


        # 5. Execute command
        if dry_run:
            # Format command for readability
            pretty_cmd = ' '.join(f"'{arg}'" if ' ' in arg else arg for arg in ffmpeg_cmd)
            logging.info(f"[DRY RUN] Would run command: {pretty_cmd}")
            return

        process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding='utf-8')
        
        output_lines = []
        is_debug_enabled = logging.getLogger().isEnabledFor(logging.DEBUG)

        # Log ffmpeg output in real-time if debug is enabled, and collect all output
        for line in process.stdout:
            stripped_line = line.strip()
            if is_debug_enabled:
                logging.debug(stripped_line)
            output_lines.append(stripped_line)

        process.wait()

        if process.returncode == 0:
            logging.info(f"Successfully transcoded '{destination_path}'")
        else:
            # On failure, log the command and the full output at ERROR level
            pretty_cmd = ' '.join(f"'{arg}'" if ' ' in arg else arg for arg in ffmpeg_cmd)
            full_output = "\n".join(output_lines)
            
            error_message = (
                f"Failed to transcode '{local_source_path}'.\n"
                f"  Exit Code: {process.returncode}\n"
                f"  Command: {pretty_cmd}\n"
                f"  FFmpeg Output:\n"
                f"{full_output}"
            )
            logging.error(error_message)

            # Send email notification
            send_failure_email(
                to_addr=mail_to,
                from_addr=mail_from,
                video_title=video.title,
                source_path=local_source_path,
                error_message=error_message
            )

    except Exception as e:
        logging.error(f"An error occurred while processing '{video.title}': {e}", exc_info=True)
    
    finally:
        # Ensure the temporary subtitle file is always cleaned up
        if temp_sub_path and os.path.exists(temp_sub_path):
            logging.info(f"Cleaning up temporary subtitle file: {temp_sub_path}")
            os.remove(temp_sub_path)




def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Transcode Plex videos from 'Van' playlists.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        '-c', '--config-file',
        type=str,
        help='Path to a configuration file.'
    )
    parser.add_argument(
        '--plex-url',
        type=str,
        help='The URL of the Plex server.'
    )
    parser.add_argument(
        '--plex-token',
        type=str,
        help='The Plex API token for authentication.'
    )
    parser.add_argument(
        '--media-dir',
        type=str,
        help='The base directory of the Plex library AS SEEN BY PLEX (e.g., /shared/plex-library/).'
    )
    parser.add_argument(
        '--local-media-dir',
        type=str,
        help='(Optional) The base directory of the Plex library on the local machine running this script. '
             'Use this if the path is different from what Plex sees. If omitted, assumes the path is the same as --media-dir.'
    )
    parser.add_argument(
        '--output-dir',
        type=str,
        help='The directory where transcoded files will be stored (e.g., /shared/plex-library/vanlife).'
    )
    parser.add_argument(
        '--log-file',
        type=str,
        help='Path to a file for logging output.'
    )
    parser.add_argument(
        '--mail-to',
        type=str,
        help='Email address to send failure notifications to.'
    )
    parser.add_argument(
        '--mail-from',
        type=str,
        help='Email address to send failure notifications from.'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='If set, print the ffmpeg commands without executing them.'
    )
    parser.add_argument(
        '--use-qsv',
        action='store_true',
        help='If set, attempt to use Intel QSV for HEVC hardware transcoding.'
    )
    parser.add_argument(
        '--reverse-sort',
        action='store_true',
        help='If set, process files in reverse sorted order (Z-A) instead of A-Z.'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging to see full ffmpeg output.'
    )

    args = parser.parse_args()

    # --- Configuration Loading ---
    config = {}
    if args.config_file:
        if not os.path.exists(args.config_file):
            print(f"Error: Config file not found at {args.config_file}")
            sys.exit(1)
        
        config_parser = configparser.ConfigParser()
        config_parser.read(args.config_file)
        
        if 'Plex' in config_parser:
            config.update(dict(config_parser['Plex']))
        if 'Paths' in config_parser:
            config.update(dict(config_parser['Paths']))
        if 'Mail' in config_parser:
            config.update(dict(config_parser['Mail']))

    # Override config file values with command-line arguments
    cli_args = {
        'url': args.plex_url,
        'token': args.plex_token,
        'media_dir': args.media_dir,
        'local_media_dir': args.local_media_dir,
        'output_dir': args.output_dir,
        'log_file': args.log_file,
        'mail_to': args.mail_to,
        'mail_from': args.mail_from,
    }
    config.update({k: v for k, v in cli_args.items() if v is not None})

    # If local_media_dir is not specified, it defaults to media_dir
    if 'local_media_dir' not in config or not config['local_media_dir']:
        config['local_media_dir'] = config.get('media_dir')


    # --- Logging Setup ---
    log_level = logging.DEBUG if args.debug else logging.INFO
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    
    # Get the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Create a formatter
    formatter = logging.Formatter(log_format)

    # Always add a StreamHandler for console output
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # If a log file is specified, add a FileHandler
    if config.get('log_file'):
        file_handler = logging.FileHandler(config.get('log_file'))
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # --- Parameter Validation ---
    required_params = ['url', 'token', 'media_dir', 'output_dir']
    missing_params = [param for param in required_params if not config.get(param)]

    if missing_params:
        logging.error(f"Missing required configuration parameters: {', '.join(missing_params)}")
        sys.exit(1)

    logging.info("Configuration loaded successfully.")
    if args.dry_run:
        logging.info("Running in DRY-RUN mode.")

    # --- Main Logic ---
    try:
        logging.info(f"Connecting to Plex server at {config['url']}...")
        plex = PlexServer(config['url'], config['token'])
        
        videos_to_transcode = get_plex_videos(plex, args.reverse_sort)
        
        if not videos_to_transcode:
            logging.info("No videos found in 'Van' playlists.")
            return

        logging.info(f"Found {len(videos_to_transcode)} videos to potentially transcode.")

        with tqdm(total=len(videos_to_transcode), desc="Transcoding Videos") as pbar:
            for video in videos_to_transcode:
                transcode_video(
                    video,
                    plex,
                    config,
                    args.dry_run,
                    args.use_qsv
                )
                pbar.update(1)

        logging.info("Script finished.")

    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
