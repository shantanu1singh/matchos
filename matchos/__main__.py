"""Start the MatchOS web server via python -m matchos or the matchos command."""

import uvicorn


def main() -> None:
    uvicorn.run("matchos.server:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
