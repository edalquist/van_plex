#!/usr/bin/env python3

import argparse
import configparser
import logging
import os
import subprocess
import sys
import re
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


def get_plex_videos(plex: PlexServer) -> List[Video]:
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
    sorted_videos = sorted(list(all_videos), key=lambda video: video.media[0].parts[0].file)
            
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

def is_transcode_valid(source_path: str, dest_path: str) -> bool:
    """
    Checks if a transcoded file is valid by comparing its duration to the source file's duration.
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
        logging.warning(f"Could not validate duration for '{dest_path}', assuming it's invalid.")
        return False

    # Check if durations are within a 1-second tolerance
    if abs(source_duration - dest_duration) < 1.0:
        return True
    else:
        logging.warning(
            f"Validation failed for '{dest_path}'. "
            f"Source duration: {source_duration:.2f}s, "
            f"Destination duration: {dest_duration:.2f}s. "
            "File will be re-transcoded."
        )
        return False

def transcode_video(
    video: Video,
    plex: PlexServer,
    media_dir: str,
    output_dir: str,
    dry_run: bool = False,
    use_qsv: bool = False
):
    """
    Manages the transcoding of a single video file.
    """
    try:
        source_path = video.media[0].parts[0].file
        
        # 1. Construct destination path
        relative_path = os.path.relpath(source_path, media_dir)
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

        # 2. Check if file is valid
        if is_transcode_valid(source_path, destination_path):
            logging.info(f"SKIPPING: Valid transcoded file already exists at '{destination_path}'")
            return

        # 3. Create destination directory
        os.makedirs(dest_dir, exist_ok=True)


        # 4. Construct ffmpeg command
        ffmpeg_cmd = [
            'ffmpeg',
            '-i', source_path
        ]

        # Video filters
        video_filters = "scale=w=1280:h=720:force_original_aspect_ratio=decrease,pad=w=1280:h=720:x=(ow-iw)/2:y=(oh-ih)/2"
        
        # Subtitle filters
        subtitle_key = find_english_subtitle_stream(video, plex)
        if subtitle_key:
            # Use Plex's transcoder endpoint for subtitles
            subtitle_url = plex.url(subtitle_key) + f"?X-Plex-Token={plex._token}"
            # Subtitles must be the first filter. We need to escape the subtitle file path for ffmpeg
            video_filters = f"subtitles='{subtitle_url}',{video_filters}"

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
        
        logging.info(f"Transcoding '{source_path}' to '{destination_path}'")


        # 5. Execute command
        if dry_run:
            # Format command for readability
            pretty_cmd = ' '.join(f"'{arg}'" if ' ' in arg else arg for arg in ffmpeg_cmd)
            logging.info(f"[DRY RUN] Would run command: {pretty_cmd}")
            return

        process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        
        # Log ffmpeg output in real-time
        for line in process.stdout:
            logging.debug(line.strip())

        process.wait()

        if process.returncode == 0:
            logging.info(f"Successfully transcoded '{destination_path}'")
        else:
            logging.error(f"Failed to transcode '{source_path}'. ffmpeg exited with code {process.returncode}.")

    except Exception as e:
        logging.error(f"An error occurred while processing '{video.title}': {e}", exc_info=True)



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
        help='The base directory of the Plex library (e.g., /shared/plex-library/).'
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
        '--dry-run',
        action='store_true',
        help='If set, print the ffmpeg commands without executing them.'
    )
    parser.add_argument(
        '--use-qsv',
        action='store_true',
        help='If set, attempt to use Intel QSV for HEVC hardware transcoding.'
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

    # Override config file values with command-line arguments
    cli_args = {
        'url': args.plex_url,
        'token': args.plex_token,
        'media_dir': args.media_dir,
        'output_dir': args.output_dir,
        'log_file': args.log_file,
    }
    # a little bit of snake_case to kebab-case conversion
    config.update({k: v for k, v in cli_args.items() if v is not None})

    # --- Logging Setup ---
    log_level = logging.INFO
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
        
        videos_to_transcode = get_plex_videos(plex)
        
        if not videos_to_transcode:
            logging.info("No videos found in 'Van' playlists.")
            return

        logging.info(f"Found {len(videos_to_transcode)} videos to potentially transcode.")

        with tqdm(total=len(videos_to_transcode), desc="Transcoding Videos") as pbar:
            for video in videos_to_transcode:
                transcode_video(
                    video,
                    plex,
                    config['media_dir'],
                    config['output_dir'],
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
