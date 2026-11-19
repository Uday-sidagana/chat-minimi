import argparse
import uvicorn


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8010)
    args = p.parse_args()
    from .server import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)
