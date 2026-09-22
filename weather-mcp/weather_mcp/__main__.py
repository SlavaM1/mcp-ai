import uvicorn

from weather_mcp.server import app


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8001, log_config=None, access_log=True)


if __name__ == "__main__":
    main()
