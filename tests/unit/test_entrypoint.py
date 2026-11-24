from sabor_da_maria_agent import main
from sabor_da_maria_agent.infrastructure.mcp import server


def test_main_starts_mcp_server_over_stdio(monkeypatch):
    calls: list[str] = []

    def fake_run(*, transport: str) -> None:
        calls.append(transport)

    monkeypatch.setattr(server.mcp, "run", fake_run)

    main()

    assert calls == ["stdio"]
