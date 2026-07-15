#!/bin/sh
set -eu

source_dir=${1:-/opt/domain-router-ui/source}
venv_dir=/opt/domain-router-ui/venv
libexec_dir=/usr/local/libexec/domain-router

if [ "$(id -u)" -ne 0 ]; then
    echo "This installer must run as root" >&2
    exit 1
fi

if [ ! -f "$source_dir/pyproject.toml" ]; then
    echo "Project source not found at $source_dir" >&2
    exit 1
fi

if ! getent group domain-router-ui >/dev/null; then
    groupadd --system domain-router-ui
fi
if ! getent passwd domain-router-ui >/dev/null; then
    useradd --system --gid domain-router-ui --home-dir /nonexistent --shell /usr/sbin/nologin domain-router-ui
fi

install -d -m 0755 /opt/domain-router-ui
install -d -m 0755 "$libexec_dir"
install -d -o domain-router-ui -g domain-router-ui -m 0700 /var/lib/domain-router-ui

if [ ! -x "$venv_dir/bin/python" ]; then
    python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/pip" install --disable-pip-version-check --upgrade "$source_dir"

ln -sfn "$venv_dir/bin/domain-router" "$libexec_dir/domain-router"
cat >/usr/local/bin/domain-router <<'EOF'
#!/bin/sh
set -a
if [ -r /etc/domain-router/environment ]; then
    . /etc/domain-router/environment
fi
set +a
exec /usr/local/libexec/domain-router/domain-router "$@"
EOF
chmod 0755 /usr/local/bin/domain-router
ln -sfn "$venv_dir/bin/domain-router-ui-admin" /usr/local/bin/domain-router-ui-admin

install -m 0644 "$source_dir/config/systemd/domain-router-ui.service" \
    /etc/systemd/system/domain-router-ui.service
install -m 0440 "$source_dir/config/sudoers/domain-router-ui" \
    /etc/sudoers.d/domain-router-ui
visudo -cf /etc/sudoers.d/domain-router-ui

systemctl daemon-reload
systemctl enable domain-router-ui.service
systemctl restart domain-router-ui.service
