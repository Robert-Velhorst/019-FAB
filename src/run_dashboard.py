from src.config_loader import ConfigLoader
from src.dashboard.app import create_app
from waitress import serve


def main() -> None:
    config = ConfigLoader(config_file="config/config.ini").get_all_config()
    app_config = config.get("app", {})
    dashboard_config = config.get("dashboard", {})
    merged_config = {**app_config, **dashboard_config, **config.get("database", {})}

    host = dashboard_config.get("host", "127.0.0.1")
    port = int(dashboard_config.get("port", 5001))
    threads = int(dashboard_config.get("threads", 4))
    if not 1 <= threads <= 64:
        raise ValueError("Dashboard threads must be between 1 and 64.")
    app = create_app(merged_config)
    serve(app, host=host, port=port, threads=threads)


if __name__ == "__main__":
    main()
