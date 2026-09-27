import uvicorn

from currency_mcp.server import app


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8002, log_config=None, access_log=True)


if __name__ == "__main__":
    main()
