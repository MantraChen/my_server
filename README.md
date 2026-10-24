# my_server

Source and deployment configuration for the services hosted at dyyjs.com.

Production changes are made through pull requests. The protected `main` branch
runs CI and deploys only changed components after an approved merge.

Secrets, local agent files, server state, Minecraft world data, and generated
release packages are intentionally excluded from this repository.

The optional [Dev Blog component](z_my_server/dev-blog/README.md) runs the pinned
Howie blog on its own Node service and SQLite data directory. It stays disabled
until the server owner completes provisioning and explicitly enables it.

