# Public server inventory

Revalidated through authorized SSH aliases on 2026-10-07. IP addresses, MACs, host-key fingerprints and identifying hostnames are deliberately omitted. No host mutation, package installation, container start or migration was performed.

| Fact | DATA_HOST | APP_HOST |
|---|---|---|
| Architecture | x86_64 | x86_64 |
| OS | Ubuntu 24.04.5 LTS | Ubuntu 26.04.1 LTS |
| Logical CPUs | 8 | 16 |
| Memory / swap | 15 GiB / 4 GiB | 13 GiB / 4 GiB |
| Root filesystem | 468 GiB, 17 GiB used, 428 GiB available | 468 GiB, 17 GiB used, 427 GiB available |
| Noninteractive sudo | sudo -n true passed | sudo -n true passed |
| Docker / Compose | 29.8.2 / v5.6.0 | 29.8.2 / v5.6.0 |
| Docker root | /var/lib/docker, rootful | /var/lib/docker, rootful |
| NTP | NTPSynchronized=yes | NTPSynchronized=yes |
| TCP listeners | SSH; loopback DNS and CUPS | SSH; loopback DNS and CUPS |
| Target ports | 55432/56379/8443/8080 not listening | same |
| Firewall | UFW active, SSH from management LAN only | same |

Executed remotely: `uname -m`, `/etc/os-release` PRETTY_NAME, `nproc`, `free -h`, `df -h /`, `sudo -n true`, `sudo -n ufw status`, `docker info --format '{{.ServerVersion}} {{.DockerRootDir}}'`, `docker compose version`, `timedatectl show -p NTPSynchronized`, `ss -lntu`.

Private handoff additionally records Wi-Fi interfaces, router DHCP reservations, laptop AC/lid settings, root-owned data/backup directories and successful registry-mirror tests. Router reservation is the owner's statement, not independently measured here. Registry reachability, SMART/lid settings and lack of existing containers were not re-tested in this command set. Docker Hub requires the verified mirror per handoff; never assume direct connectivity. Wi-Fi is not a measured gigabit wired link. Current rootful Docker does not meet future rootless CI isolation; NX-033 needs a verified implementation/ADR. Existing base directories need per-container UID allocation at deployment. No business backup or restore acceptance is claimed.
