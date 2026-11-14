# Deployment configurator

Open setup.html directly in a modern browser. The page runs offline and generates wol.env, mysql.json, wol-gateway.service, and wol-enrollment.service. It does not store or transmit submitted values.

This is a configuration generator, not a remote installer. Before enabling the units:

1. Create a dedicated Linux service account and install the project and Python dependencies under the selected project directory.
2. Create the MySQL database and a least-privilege account. Store mysql.json under /etc/wol-gateway/ with owner root, group set to the service account, and mode 0640 (or stricter where practical). Store wol.env with the same restrictions. Create /var/lib/wol-gateway and grant write access only to the service account.
3. Configure Mosquitto authentication and per-device ACLs, TLS on the public listener, and a local authenticated website account. Do not expose the local listener.
4. Configure Nginx and valid certificates for the website and enrollment HTTPS endpoints. Allow only required public ports; do not expose MySQL or the local MQTT listener.
5. Configure the selected domain and trust chain in the ESP32 and Windows client builds. Firmware OTA also needs a signing key; keep that private key outside the repository and backups intended for public release.
6. Place generated units under /etc/systemd/system/, inspect them, then reload systemd and start the services. Configure monitoring and tested database backups before adding real gateways.

The sample unit uses the ASGI module names used by this project. Review paths, the database service dependency, and hardening directives against the target distribution before use. The page intentionally does not generate firewall commands or execute privileged actions.
