import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

from agent_system.settings import ROOT_DIR

load_dotenv(dotenv_path=ROOT_DIR / ".env")


def main() -> None:
    parser = argparse.ArgumentParser(description="LangChain multi-agent micropayment client")
    parser.add_argument("query", nargs="*", help="Natural language task for the agents")
    parser.add_argument("--count", type=int, choices=[5, 10], help="Direct purchase without LLM")
    args = parser.parse_args()

    if args.count:
        from agent_system.graph import run_direct_purchase

        result = run_direct_purchase(args.count)
        print(json.dumps(result, indent=2))
        return

    query = " ".join(args.query).strip() or "What data providers are available and how many numbers do they offer?"
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set. Use --count 5 or --count 10 for direct mode.", file=sys.stderr)
        sys.exit(1)

    from agent_system.graph import run_multi_agent

    result = asyncio.run(run_multi_agent(query))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
