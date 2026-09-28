# Open Terminal 0.14.0, pinned to its published multi-architecture digest.
FROM ghcr.io/open-webui/open-terminal@sha256:81a5394b3cd4ae32adb600f2135f09ee124de37f26b0a780e2f5692472c0fc5c

USER root
# The upstream launcher uses a capability-bearing Python interpreter. Linux
# rejects that binary under no-new-privileges with all capabilities dropped.
# Single-admin mode does not need setgid or passwordless sudo.
RUN setcap -r /usr/local/bin/python3-ot \
    && sed -i '/^user ALL=(ALL) NOPASSWD:ALL$/d' /etc/sudoers \
    && ! getcap /usr/local/bin/python3-ot | grep -q cap_setgid \
    && ! grep -q '^user ALL=(ALL) NOPASSWD:ALL$' /etc/sudoers

USER user
