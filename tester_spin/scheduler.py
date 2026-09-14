from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable, Iterable

from tester_spin.farm_contract import export_farm_contract
from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import ProviderAdapter

Progress = Callable[[str], None]
ResultCallback = Callable[[GameTestResult], None]


def run_game_tests(
    provider: ProviderAdapter,
    games: Iterable[Game],
    *,
    concurrency: int,
    spins_per_game: int,
    delay_between_starts_s: float,
    timeout_s: float,
    stop_event: threading.Event,
    progress: Progress,
    on_result: ResultCallback,
) -> None:
    queue = list(games)
    if not queue:
        return

    requested_concurrency = max(1, int(concurrency))
    concurrency = provider.effective_test_concurrency(requested_concurrency)
    if concurrency != requested_concurrency:
        progress(
            f"{provider.display_name}: concurrencia solicitada={requested_concurrency}, "
            f"límite seguro del proveedor={concurrency}; se ejecutará en serie."
        )
    spins_per_game = max(1, int(spins_per_game))
    delay_between_starts_s = max(0.0, float(delay_between_starts_s))
    timeout_s = max(1.0, float(timeout_s))

    def worker(game: Game) -> GameTestResult:
        game_progress = lambda msg: progress(f"[{game.name}] {msg}")
        try:
            provider.prepare_test_artifacts(
                game,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=game_progress,
            )
        except Exception as exc:
            # Diagnostic preparation is best-effort and must never turn an
            # otherwise valid provider test into an ERROR.
            game_progress(
                f"preparación de artefactos ERROR; la prueba continúa: "
                f"{type(exc).__name__}: {exc}"
            )
        if stop_event.is_set():
            return GameTestResult(
                provider=game.provider,
                slug=game.slug,
                game_name=game.name,
                game_url=game.url,
                requested_spins=spins_per_game,
                successful_spins=0,
                failed_spins=spins_per_game,
                status="CANCELADO",
                symbol=game.symbol,
                error="Detención solicitada durante preparación de artefactos.",
            )
        result = provider.test_game(
            game,
            spins=spins_per_game,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=game_progress,
        )
        result.samples_per_path = spins_per_game
        result = provider.finalize_test_result(result, progress=game_progress)
        export_farm_contract(
            provider,
            game,
            result,
            progress=game_progress,
        )
        return result

    in_flight: dict[Future[GameTestResult], Game] = {}
    next_index = 0
    last_start = 0.0

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="game-test") as pool:
        while (next_index < len(queue) or in_flight) and not stop_event.is_set():
            while next_index < len(queue) and len(in_flight) < concurrency and not stop_event.is_set():
                if last_start and delay_between_starts_s > 0:
                    remaining = delay_between_starts_s - (time.monotonic() - last_start)
                    if remaining > 0 and stop_event.wait(remaining):
                        break

                game = queue[next_index]
                next_index += 1
                progress(
                    f"Iniciando {next_index}/{len(queue)}: {game.name} "
                    f"(activos={len(in_flight) + 1}/{concurrency})"
                )
                future = pool.submit(worker, game)
                in_flight[future] = game
                last_start = time.monotonic()

            if not in_flight:
                continue

            done, _ = wait(tuple(in_flight), timeout=0.25, return_when=FIRST_COMPLETED)
            for future in done:
                game = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = GameTestResult(
                        provider=game.provider,
                        slug=game.slug,
                        game_name=game.name,
                        game_url=game.url,
                        requested_spins=spins_per_game,
                        successful_spins=0,
                        failed_spins=spins_per_game,
                        status="ERROR",
                        symbol=game.symbol,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                on_result(result)

        if stop_event.is_set():
            progress("Detención solicitada; esperando las pruebas que ya estaban en vuelo...")
            for future, game in list(in_flight.items()):
                try:
                    result = future.result()
                except Exception as exc:
                    result = GameTestResult(
                        provider=game.provider,
                        slug=game.slug,
                        game_name=game.name,
                        game_url=game.url,
                        requested_spins=spins_per_game,
                        successful_spins=0,
                        failed_spins=spins_per_game,
                        status="CANCELADO",
                        symbol=game.symbol,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                on_result(result)
