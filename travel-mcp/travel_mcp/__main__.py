import uvicorn

from travel_mcp.server import app


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8003, log_config=None, access_log=True)


if __name__ == "__main__":
    main()
