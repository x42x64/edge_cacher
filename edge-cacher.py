#!/usr/bin/python3
import grp
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import click

_BASE_DIR = Path(os.environ.get("HOME", "/root")) / Path('.config/edge-cacher')
_UNMOUNT_SCRIPT = (Path(__file__).parent /
                   "rclone_umount.sh").absolute().as_posix()
_RCLONE_BINARY = "/usr/bin/rclone"
_SAMBA_CONFIG = Path("/etc/samba/smb.conf")
_GROUP_NAME = "edge_cacher"


@dataclass
class RemoteConfig:
    url: str
    username: str
    password: str


@dataclass
class VfsConfig:
    cache_dir: str
    vfs_cache_max_size: str
    vfs_cache_max_age: str = "8760h"
    vfs_cache_mode: str = "full"
    vfs_cache_poll_interval: str = "1m"
    vfs_write_back: str = "5s"
    vfs_read_chunk_size: str = "64M"
    buffer_size: str = "16M"
    vfs_read_ahead: str = "512M"
    poll_interval: str = "30s"
    dir_cache_time: str = "5m"
    attr_timeout: str = "1s"


@dataclass
class SmbConfig:
    password: str


@dataclass
class EdgeCacherConfig:
    share_name: str
    remote: RemoteConfig
    vfs: VfsConfig
    smb: SmbConfig = None

    def __post_init__(self):
        if self.share_name.strip() == "":
            raise ValueError(f"share_name must consist not only of blanks!")


def get_mount_path(share_name: str) -> Path:
    return Path(f"/mnt/edge-cacher/{share_name}")


def get_rclone_config_path(share_name: str) -> Path:
    return _BASE_DIR / share_name / "rclone.conf"


def get_smb_config_path(share_name: str) -> Path:
    return _BASE_DIR / share_name / "smb.conf"


def get_service_unit_path(share_name: str) -> Path:
    return _BASE_DIR / share_name / f"edge-cacher-{share_name}.service"


def create_smb_config(smb_config: SmbConfig, share_name: str):
    with get_smb_config_path(share_name).open(mode='w+') as fd:
        fd.write(f"""
[{share_name}]
   path = {get_mount_path(share_name).as_posix() }
   comment =
   guest ok = no
   browsable = yes
   writeable = yes
   printable = no
   valid users = {share_name}_user
   create mask=0777
   directory mask=0777

""")


def is_samba_available():
    ret = False
    try:
        result = subprocess.run(["smbd", "--version"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        ret = (result.returncode == 0)
    except:
        pass
    finally:
        return ret


def create_service_unit(config: VfsConfig, share_name: str, enable_smb: bool):
    with get_service_unit_path(share_name).open(mode="w+") as fd:
        content = f"""
[Unit]
Description=Edge cacher rclone mount for {share_name}
AssertPathIsDirectory={get_mount_path(share_name).as_posix()}
After=network-online.target

[Service]
Type=notify
ExecStart={_RCLONE_BINARY} mount \
        -v \
        --config={get_rclone_config_path(share_name)} \
        --allow-other \
        --cache-dir {config.cache_dir} \
        --vfs-cache-mode full \
        --vfs-cache-max-age {config.vfs_cache_max_age} \
        --vfs-cache-max-size {config.vfs_cache_max_size} \
        --vfs-cache-poll-interval {config.vfs_cache_poll_interval} \
        --vfs-write-back {config.vfs_write_back} \
        --vfs-read-chunk-size {config.vfs_read_chunk_size} \
        --buffer-size {config.buffer_size} \
        --vfs-read-ahead {config.vfs_read_ahead} \
        --poll-interval {config.poll_interval} \
        --dir-cache-time {config.dir_cache_time} \
        --attr-timeout {config.attr_timeout} \
        --umask 777 \
        --syslog \
        {share_name}: {get_mount_path(share_name)}
ExecStop={_UNMOUNT_SCRIPT} {get_mount_path(share_name)}
Restart=always
RestartSec=10

        """

        if enable_smb:
            content += """
[Install]
RequiredBy=smbd.service
"""
        else:
            content += """
[Install]
WantedBy=multi-user.target
"""

        fd.write(content)


def rclone_obscure_password(password: str):
    if not password:
        raise ValueError("Passwort must not be empty!")

    result = subprocess.run([_RCLONE_BINARY, 'obscure', password],
                            stdout=subprocess.PIPE)

    if result.returncode != 0:
        raise OSError(f"Obsucring password using {_RCLONE_BINARY} failed!")
    return result.stdout.decode("UTF-8")


def create_rclone_config(remote_config: RemoteConfig, share_name: str):
    with get_rclone_config_path(share_name).open(mode='w+') as fd:
        fd.write(f"""
[{share_name}]
type = webdav
url = {remote_config.url}
vendor = nextcloud
user = {remote_config.username}
pass = {rclone_obscure_password(remote_config.password)}
""")


def ensure_group_existing():
    if _GROUP_NAME not in [g.gr_name for g in grp.getgrall()]:
        result = subprocess.run(["groupadd", "--system", _GROUP_NAME])
        if result.returncode != 0:
            raise OSError("Could not create group edge_cache.")


def add_user(username: str, password: str):
    # check if group exists
    ensure_group_existing()

    result = subprocess.run([
        "useradd", "-G", _GROUP_NAME, "--system", "--no-create-home",
        "--shell", "/bin/false", username
    ])
    if result.returncode != 0:
        raise OSError(f"Could not create user {username}.")

    result = subprocess.run([
        "bash", "-c",
        f"printf '{password}\n{password}\n' | sudo pdbedit -t -a -u {username}"
    ],
                            stdout=subprocess.PIPE)
    if result.returncode != 0:
        raise OSError(
            f"Could not set password for samba user {username}. Output: \n{result.stdout}"
        )


def delete_user(username):
    # remove from samba database
    result = subprocess.run(["pdbedit", "-x", username])
    if result.returncode != 0:
        raise OSError(f"Could not delete {username} from samba database.")

    # remove user
    result = subprocess.run(["userdel", username])
    if result.returncode != 0:
        raise OSError(f"Could not delete {username} from system.")


def register_rclone_service(share_name: str):
    local_path = get_service_unit_path(share_name)

    result = subprocess.run(["systemctl", "enable", local_path.as_posix()])
    if result.returncode != 0:
        raise OSError(f"Could not enable {local_path.name}.")

    result = subprocess.run(["systemctl", "start", local_path.name])
    if result.returncode != 0:
        raise OSError(f"Could not start {local_path.name}.")


def unregister_rclone_service(share_name: str):
    link_path = get_service_unit_path(share_name)

    result = subprocess.run(["systemctl", "stop", link_path.name])
    if result.returncode != 0:
        raise OSError(f"Could not start {link_path.name}.")

    result = subprocess.run(["systemctl", "disable", link_path.name])
    if result.returncode != 0:
        raise OSError(f"Could not enable {link_path.name}.")


def restart_samba():
    result = subprocess.run(["systemctl", "restart", "smbd.service"])
    if result.returncode != 0:
        raise OSError("Could not restart smbd.service")


def add_remote(config: EdgeCacherConfig):
    remote_config_dir = _BASE_DIR / config.share_name
    if remote_config_dir.exists():
        raise OSError(f"Remote {remote_config_dir} already exists!")

    ensure_group_existing()

    remote_config_dir.mkdir(exist_ok=False, parents=True)
    mount_path = get_mount_path(config.share_name)
    mount_path.mkdir(exist_ok=False, parents=True)
    shutil.chown(mount_path, None, _GROUP_NAME)

    create_rclone_config(config.remote, config.share_name)
    create_service_unit(config.vfs, config.share_name, config.smb is not None)

    register_rclone_service(config.share_name)

    if config.smb is not None and is_samba_available():
        print("creating smb config")
        create_smb_config(config.smb, config.share_name)

        with _SAMBA_CONFIG.open("a") as f:
            f.write(
                f"\ninclude = {get_smb_config_path(config.share_name).absolute().as_posix()}"
            )

        add_user(f"{config.share_name}_user", config.smb.password)

        restart_samba()


def remove_remote(share_name):
    remote_config_dir = (_BASE_DIR / share_name).absolute()
    if not remote_config_dir.exists():
        raise OSError(f"Remote {remote_config_dir} does not exist!")

    if _BASE_DIR not in remote_config_dir.parents:
        raise OSError(
            f"Remote {remote_config_dir} is not in the Base dir {_BASE_DIR}!")

    if get_smb_config_path(share_name).exists():
        # remove share from smb
        with _SAMBA_CONFIG.open("r") as f:
            lines = f.readlines()
        with _SAMBA_CONFIG.open("w") as f:
            for line in lines:
                if get_smb_config_path(
                        share_name).absolute().as_posix() not in line:
                    f.write(line)

        restart_samba()

        delete_user(f"{share_name}_user")

    unregister_rclone_service(share_name)
    shutil.rmtree(remote_config_dir)
    mount_dir = get_mount_path(share_name)

    files_in_mount_dir = list(mount_dir.glob("*"))
    if len(files_in_mount_dir) > 0:
        raise OSError(
            f"mount directory {mount_dir} is not empty: {files_in_mount_dir} - Will not delete!"
        )
    mount_dir.rmdir()


@click.group()
def main():
    """
    A tool to configure rclone and systemd to mount nextcloud webdav shares and potentially serve them via samba.
    """
    print("Checking for rclone:")
    result = subprocess.run([_RCLONE_BINARY, "--version"],
                            stdout=subprocess.PIPE)
    if result.returncode != 0:
        raise OSError(f"Cannot run {_RCLONE_BINARY}")
    else:
        print("Ok")
    print()

    print("Checking for systemctl")
    result = subprocess.run(["systemctl", "--version"], stdout=subprocess.PIPE)
    if result.returncode != 0:
        raise OSError(
            f"Cannot execute systemctl. This only works where systemctl is available!"
        )
    else:
        print("Ok")
    print()

    print("Checking for samba:")
    print("---------------------")
    print(
        f"The samba service is {'' if is_samba_available() else 'not '}running."
    )
    print("---------------------")
    print()


@main.command()
@click.argument("configfile", type=click.Path(exists=True))
def add(configfile: str):
    """
    Add an edge cache by providing a JSON config as CONFIGFILE
    """
    with Path(configfile).open() as fd:
        config_json = json.load(fd)

    config = create_config(config_json)
    add_remote(config)

def create_config(config_json):
    config = EdgeCacherConfig(
        share_name=config_json["share_name"],
        remote=RemoteConfig(**config_json["remote"]),
        vfs=VfsConfig(**config_json["vfs"]),
        smb=SmbConfig(**config_json["smb"]) if "smb" in config_json else None)
    return config


@main.command()
@click.argument("share-name")
def remove(share_name: str):
    """
    Remove an edge cache by its name like displayed with `ls`.
    """
    remove_remote(share_name)


@main.command()
@click.argument("configfile", type=click.Path(exists=True))
def update(configfile: str):
    """
    Update (remove and add) an edge cache by providing a JSON config as CONFIGFILE
    Note: The share name must be the same. If this is not the case. Remove the old share and then add the new config file
    """
    with Path(configfile).open() as fd:
        config_json = json.load(fd)
    remove_remote(config_json["share_name"])
    config = create_config(config_json)
    add_remote(config)


@main.command()
def ls():
    """
    List all configured edge caches by this user.
    """
    print(
        f"Following remotes are registered using this edge-cacher ({_BASE_DIR}):"
    )
    print()
    print("\n".join([p.name for p in _BASE_DIR.glob("*") if p.is_dir()]))
    print()


if __name__ == "__main__":
    main()
