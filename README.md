# Vanlife Project

This repository contains scripts related to managing media for a vanlife setup.

## pi-sync.py

This script is designed to synchronize media files from a local server to a Raspberry Pi (van-media.home.dalquist.org) every time the Pi comes online on the network. It uses `rsync` over SSH to keep directories synchronized, including deleting files from the destination that no longer exist in the source.

### Setup and Configuration

1.  **Configure `pi_sync.ini`**:
    All configuration for the script is handled in the `pi_sync.ini` file.

    **`[Sync]` Section:**
    *   `RemoteHost`: The hostname or IP address of your Raspberry Pi.
    *   `User`: The SSH username to use for the connection (e.g., `pi`).
    *   `SSHKeyFile`: The path to the SSH private key to use for the connection. If left blank, the system's default key will be used.
    *   `CheckIntervalSeconds`: How often (in seconds) the script should check if the Pi is online. Defaults to 60.
    *   `SyncDirs`: A JSON object mapping local source paths to remote destination paths.
    *   `LockFile`: The path to the lock file to prevent concurrent runs.
    *   `LogFile`: The path to the log file.

    Example for `SyncDirs`:
    ```ini
    SyncDirs = {"/home/user/my_music": "Music/", "/home/user/my_videos": "Videos/"}
    ```

    **`[Email]` Section (Optional):**
    *   `To`: The recipient's email address.
    *   `From`: The sender's email address.
    *   `Subject`: The subject line for the sync summary email.

    If you do not want email notifications, simply leave the `To` field blank.

2.  **SSH Key Authentication**:
    Ensure that the local server can connect to the Raspberry Pi via SSH without a password using SSH keys. If you haven't set this up, generate an SSH key pair on your local server and copy the public key to your Raspberry Pi:
    ```bash
    ssh-keygen -t rsa -b 4096 # Follow prompts
    ssh-copy-id <user>@van-media.home.dalquist.org
    ```

### Deploying with systemd

To run `pi_sync.py` as a background service and have it start automatically on boot, you can use `systemd`.

1.  **Make the script executable**:
    ```bash
    chmod +x pi_sync.py
    ```

2.  **Move the service file**:
    Move the generated `pi-sync.service` file to the systemd service directory. Replace `<path_to_your_project>` with the actual path to this project directory (e.g., `/Users/edalquist/projects/vanlife`).

    ```bash
    sudo cp <path_to_your_project>/pi-sync.service /etc/systemd/system/
    ```

    *Self-correction: The `pi-sync.service` file already contains the full path, so this instruction is simpler.*

    ```bash
    sudo mv pi-sync.service /etc/systemd/system/
    ```

3.  **Reload systemd**:
    Inform systemd about the new service:

    ```bash
    sudo systemctl daemon-reload
    ```

4.  **Enable the service**:
    Configure the service to start automatically on boot:

    ```bash
    sudo systemctl enable pi-sync.service
    ```

5.  **Start the service**:
    Start the service immediately:

    ```bash
    sudo systemctl start pi-sync.service
    ```

6.  **Check status (optional)**:
    To check if the service is running and view its logs:

    ```bash
    systemctl status pi-sync.service
    journalctl -u pi-sync.service -f
    ```
