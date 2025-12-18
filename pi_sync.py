#!/usr/bin/env python3

import configparser
import json
import logging
import re
import subprocess
import time
from email.message import EmailMessage
from pathlib import Path


def load_config(path="pi_sync.ini"):
    """Loads the configuration from a given path."""
    if not Path(path).exists():
        logging.error(f"Config file not found at {path}")
        exit(1)
    config = configparser.ConfigParser()
    config.read(path)
    return config


def setup_logging(config):
    """Sets up logging to both file and console."""
    log_file = config.get("Sync", "LogFile", fallback="/tmp/pi_sync.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )


def is_host_online(config):
    """Check if a host is online by pinging it."""
    host = config.get("Sync", "RemoteHost")
    try:
        subprocess.run(
            ["ping", "-c", "1", host],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def create_lock(config):
    """Creates the lock file."""
    lock_file = Path(config.get("Sync", "LockFile", fallback="/tmp/pi_sync.lock"))
    if lock_file.exists():
        return False
    try:
        lock_file.touch()
        return True
    except IOError:
        return False


def remove_lock(config):
    """Removes the lock file."""
    lock_file = Path(config.get("Sync", "LockFile", fallback="/tmp/pi_sync.lock"))
    try:
        lock_file.unlink()
    except IOError:
        logging.error(f"Could not remove lock file: {lock_file}")


def send_email(config, body):
    """Sends an email with the given body using the local sendmail command."""
    email_config = config["Email"]
    recipient = email_config.get("To")

    if not recipient:
        logging.warning("Email recipient not configured. Skipping email notification.")
        return

    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = email_config.get("Subject", "Pi Sync Report")
    msg["From"] = email_config.get("From", "pi-sync@localhost")
    msg["To"] = recipient

    try:
        # Popen is used to pipe the message to sendmail's stdin
        with subprocess.Popen(
            ["sendmail", "-t"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as p:
            stdout, stderr = p.communicate(msg.as_string().encode("utf-8"))
            if p.returncode != 0:
                logging.error(f"sendmail failed with exit code {p.returncode}: {stderr.decode('utf-8')}")
            else:
                logging.info(f"Email notification sent to {recipient} via sendmail.")
    except FileNotFoundError:
        logging.error("sendmail command not found. Please ensure it's installed and in your PATH.")
    except Exception as e:
        logging.error(f"Failed to send email via sendmail: {e}")


def sync_directories(config):
    """Syncs directories using rsync."""
    sync_config = config["Sync"]
    try:
        sync_dirs = json.loads(sync_config.get("SyncDirs", "{}"))
    except json.JSONDecodeError:
        logging.error("Could not parse SyncDirs from config. It should be a valid JSON object.")
        return

    if not sync_dirs:
        logging.warning("No directories configured for syncing.")
        return

    remote_host = sync_config.get("RemoteHost")
    remote_user = sync_config.get("User")
    ssh_key_file = sync_config.get("SSHKeyFile")

    if remote_user:
        remote_target = f"{remote_user}@{remote_host}"
    else:
        remote_target = remote_host

    ssh_command = "ssh"
    if ssh_key_file:
        ssh_command = f"ssh -i {ssh_key_file}"

    full_output = []
    any_changes = False
    for src, dest in sync_dirs.items():
        logging.info(f"Syncing {src} to {remote_target}:{dest}")
        try:
            result = subprocess.run(
                [
                    "rsync",
                    "-a",
                    "--delete",
                    "--stats",
                    "--itemize-changes",
                    "-e",
                    ssh_command,
                    src,
                    f"{remote_target}:{dest}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            logging.info(f"Successfully synced {src}")
            
            output_text = result.stdout.strip()
            # Find where the summary stats start
            stats_start_index = output_text.find("Number of files:")
            
            # Check for itemized changes (anything before the stats)
            if stats_start_index > 0:
                any_changes = True
                full_output.append(f"--- Sync summary for {src} ---\n{output_text}")
            else: # No itemized changes, check the stats for transfers
                match = re.search(r"Number of files transferred: ([1-9]\d*)", output_text)
                if match:
                    any_changes = True
                    full_output.append(f"--- Sync summary for {src} ---\n{output_text}")
                else:
                    logging.info(f"No changes for {src}")

        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to sync {src}. Error: {e.stderr}")
            full_output.append(f"--- Sync FAILED for {src} ---\n{e.stderr}")
            any_changes = True

    if any_changes:
        send_email(config, "\n\n".join(full_output))
    else:
        logging.info("Sync run complete. No changes detected.")

def validate_config(config):
    """Validates the configuration."""
    try:
        config.getint("Sync", "CheckIntervalSeconds")
    except ValueError:
        logging.error("CheckIntervalSeconds in config must be a valid integer.")
        exit(1)

    try:
        json.loads(config.get("Sync", "SyncDirs", fallback="{}"))
    except json.JSONDecodeError:
        logging.error("SyncDirs in config is not valid JSON. Please check the format.")
        exit(1)
    
    return True


def main():
    """Main function to run the sync process."""
    config = load_config()
    validate_config(config)
    setup_logging(config)
    logging.info("Starting pi-sync script.")

    sync_config = config["Sync"]
    check_interval = sync_config.getint("CheckIntervalSeconds", 60)
    post_sync_interval = sync_config.getint("PostSyncSleepSeconds", 3600)
    
    sleep_interval = check_interval

    while True:
        if is_host_online(config):
            logging.info(f"Host {sync_config.get('RemoteHost')} is online.")
            if create_lock(config):
                logging.info("Lock acquired, starting sync.")
                try:
                    sync_directories(config)
                    # If sync completes, set the longer sleep interval
                    sleep_interval = post_sync_interval
                    logging.info(f"Sync process finished. Setting sleep interval to {sleep_interval} seconds.")
                finally:
                    remove_lock(config)
                    logging.info("Lock released.")
            else:
                logging.warning("Could not acquire lock. Another sync may be in progress.")
                # Host is up but busy, use short interval
                sleep_interval = check_interval
        else:
            logging.info(f"Host {sync_config.get('RemoteHost')} is offline.")
            # Host is offline, use short interval
            sleep_interval = check_interval

        logging.info(f"Sleeping for {sleep_interval} seconds.")
        time.sleep(sleep_interval)


if __name__ == "__main__":
    main()
