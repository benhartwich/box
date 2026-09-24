printf '%s\n' "$MYBOXI_TEST_PASSWORD" | myboxi-server create-admin --email "$MYBOXI_ADMIN_EMAIL" \
    --tenant-name "$MYBOXI_TENANT_NAME" --password-stdin
