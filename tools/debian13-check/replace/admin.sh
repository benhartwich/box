printf '%s\n' "$BOX_TEST_PASSWORD" | box-server create-admin --email "$BOX_ADMIN_EMAIL" \
    --tenant-name "$BOX_TENANT_NAME" --password-stdin
