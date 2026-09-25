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
  *[[:cntrl:]]* | [[:space:]]* | *[[:space:]] | *\\)
    echo "Refusing to install from this directory: systemd would read a control character, leading or trailing whitespace, or a trailing backslash in its path differently." >&2
    exit 1
    ;;
esac

UNIT_WORKING_DIRECTORY=${INSTALL_DIR//%/%%}
UNIT_EXEC_DIR=${INSTALL_DIR//\\/\\\\}
UNIT_EXEC_DIR=${UNIT_EXEC_DIR//\"/\\\"}
UNIT_EXEC_DIR=${UNIT_EXEC_DIR//%/%%}
UNIT_EXEC_DIR=${UNIT_EXEC_DIR//\$/\$\$}

cat > "$SERVICE_FILE" <<EOF_UNIT
[Unit]
Description=$SERVICE_DESCRIPTION
Documentation=$SERVICE_DOCUMENTATION
After=network.target nss-lookup.target

[Service]
ExecStart="$UNIT_EXEC_DIR/.venv/bin/python3" "$UNIT_EXEC_DIR/main.py"
Restart=on-failure
WorkingDirectory=$UNIT_WORKING_DIRECTORY

[Install]
WantedBy=multi-user.target
EOF_UNIT

systemctl daemon-reload

echo "Service file created at: $SERVICE_FILE"
