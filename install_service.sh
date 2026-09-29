#!/bin/bash

SERVICE_NAME="pasarguard"
SERVICE_DESCRIPTION="PasarGuard Service"
SERVICE_DOCUMENTATION="https://github.com/pasarguard/panel"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

resolved_dir="$(pwd -P && printf x)" || {
  echo "Could not resolve the installation directory." >&2
  exit 1
}
INSTALL_DIR=${resolved_dir%$'\n'x}

case $INSTALL_DIR in
  *[[:cntrl:]]* | [[:space:]]* | *[[:space:]] | *[\"\'\\]*)
    echo "Refusing to install from this directory: systemd refuses an executable path with a quote, a backslash or a control character, and trims leading and trailing whitespace." >&2
    exit 1
    ;;
esac

UNIT_WORKING_DIRECTORY=${INSTALL_DIR//%/%%}
UNIT_EXECUTABLE_DIR=${INSTALL_DIR//%/%%}
UNIT_ARGUMENT_DIR=${UNIT_EXECUTABLE_DIR//\$/\$\$}

cat > "$SERVICE_FILE" <<EOF_UNIT
[Unit]
Description=$SERVICE_DESCRIPTION
Documentation=$SERVICE_DOCUMENTATION
After=network.target nss-lookup.target

[Service]
ExecStart="$UNIT_EXECUTABLE_DIR/.venv/bin/python3" "$UNIT_ARGUMENT_DIR/main.py"
Restart=on-failure
WorkingDirectory=$UNIT_WORKING_DIRECTORY

[Install]
WantedBy=multi-user.target
EOF_UNIT

systemctl daemon-reload

echo "Service file created at: $SERVICE_FILE"
