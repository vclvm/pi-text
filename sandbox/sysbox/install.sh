#!/bin/bash
# Installs the sysbox runtime built by sysbox/Dockerfile into the Colima VM, the way sysbox's own
# package (sysbox-pkgr, postinst) does. `pibox up` runs it as root in the VM:
#   install.sh <dir with bin/ and systemd/> [<proxy URL for apt>]
set -euo pipefail
dir=$1 proxy=${2:-}

install -m0755 "$dir"/bin/sysbox-runc "$dir"/bin/sysbox-mgr "$dir"/bin/sysbox-fs /usr/bin/
for f in sysbox.service sysbox-mgr.service sysbox-fs.service; do
  install -D -m0644 "$dir/systemd/$f" "/etc/systemd/system/$f"
done
install -D -m0644 "$dir"/systemd/99-sysbox-sysctl.conf /etc/sysctl.d/99-sysbox-sysctl.conf
install -D -m0644 "$dir"/systemd/50-sysbox-mod.conf /etc/modules-load.d/50-sysbox-mod.conf

# What the package depends on: fusermount3 for sysbox-fs, rsync and iptables for sysbox-mgr
missing=""
for p in fuse3 rsync iptables; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
if [ -n "$missing" ]; then
  opts=()
  [ -n "$proxy" ] && opts=(-o "Acquire::http::Proxy=$proxy" -o "Acquire::https::Proxy=$proxy")
  apt-get "${opts[@]}" update -qq
  DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" install -y -qq --no-install-recommends $missing
fi

getent passwd sysbox >/dev/null || useradd -r -s /bin/false sysbox
mkdir -p /var/lib/sysboxfs
modprobe configfs 2>/dev/null || true
sysctl --system >/dev/null 2>&1 || true            # a key the kernel lacks is not an error
systemctl daemon-reload
systemctl enable --now sysbox.service

# Register the runtime with the VM's Docker. Colima rewrites daemon.json from colima.yaml at every
# start; pibox setup puts the same entry there, so it survives restarts.
python3 - <<'EOF'
import json, os
path = "/etc/docker/daemon.json"
cfg = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
cfg.setdefault("runtimes", {})["sysbox-runc"] = {"path": "/usr/bin/sysbox-runc"}
json.dump(cfg, open(path, "w"), indent=2)
EOF
docker ps -q | xargs -r docker stop >/dev/null      # restarting Docker stops them anyway
systemctl restart docker
systemctl is-active -q sysbox && docker info --format '{{json .Runtimes}}' | grep -q sysbox-runc
echo "sysbox $(cat "$dir/VERSION" 2>/dev/null) is installed in the VM"
