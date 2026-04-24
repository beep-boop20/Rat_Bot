import asyncio
import multiprocessing
import os
import queue
import signal
import socket
import sys
import time

import uvicorn
from dotenv import load_dotenv

from services.control_ipc import parse_control_message

# Load environment variables
load_dotenv()


def is_port_in_use(port: int) -> bool:
    """Checks if a port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("localhost", port)) == 0


def run_web(shared_state, command_queue, control_queue):
    """Runs the FastAPI web server."""
    sys.path.append(os.getcwd())
    from services.control_ipc import configure_control_queue
    from services.music.ipc import configure_music_ipc

    configure_music_ipc(command_queue, shared_state)
    configure_control_queue(control_queue)
    from web.app import app

    # Log level warning to suppress access logs
    uvicorn.run(app, host="0.0.0.0", port=7734, reload=False, log_level="warning")


def run_bot(shared_state, command_queue):
    """Runs the Discord Bot."""
    sys.path.append(os.getcwd())
    load_dotenv(override=True)
    from services.music.ipc import configure_music_ipc

    configure_music_ipc(command_queue, shared_state)

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("No token found. Bot will not start until token is configured in Web UI.")
        return

    try:
        from bot_service import main as bot_main

        asyncio.run(bot_main())
    except Exception as exc:
        print(f"Bot process failed: {exc}")
        raise


def stop_process(proc: multiprocessing.Process, name: str, timeout: float = 5.0) -> None:
    """Terminate and join a process safely."""
    if proc is None:
        return

    if proc.is_alive():
        proc.terminate()
        proc.join(timeout)
        if proc.is_alive():
            print(f"{name} did not stop gracefully. Killing it.")
            proc.kill()
            proc.join(timeout)
    else:
        proc.join(timeout=0)


def close_queue(ipc_queue, name: str) -> None:
    """Close a multiprocessing queue and its feeder thread."""
    if ipc_queue is None:
        return

    try:
        ipc_queue.close()
    except Exception as exc:
        print(f"Failed to close {name}: {exc}")
        return

    try:
        ipc_queue.join_thread()
    except Exception as exc:
        print(f"Failed to join {name} feeder thread: {exc}")


def start_services(shared_state, command_queue, control_queue):
    """Start web and bot child processes."""
    web_process = multiprocessing.Process(
        target=run_web,
        name="WebProcess",
        args=(shared_state, command_queue, control_queue),
    )
    web_process.start()
    print(f"Web Server started on http://localhost:7734 (PID: {web_process.pid})")

    bot_process = multiprocessing.Process(
        target=run_bot,
        name="BotProcess",
        args=(shared_state, command_queue),
    )
    bot_process.start()
    print(f"Bot process started (PID: {bot_process.pid})")

    return web_process, bot_process


def main():
    print("--- TheRatBot ---")

    # Check Port 7734 once before supervisor starts.
    if is_port_in_use(7734):
        print("\nCRITICAL ERROR: Port 7734 is already in use.")
        print("Please stop the other application using this port and try again.")
        sys.exit(1)

    print("Starting services...")

    shutdown_requested = {"value": False}

    def signal_handler(_sig, _frame):
        print("\nShutdown requested...")
        shutdown_requested["value"] = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while True:
        action = "shutdown"
        with multiprocessing.Manager() as manager:
            shared_state = manager.dict()
            command_queue = multiprocessing.Queue()
            control_queue = multiprocessing.Queue()

            web_process, bot_process = start_services(shared_state, command_queue, control_queue)
            bot_exit_notified = False

            while True:
                if shutdown_requested["value"]:
                    action = "shutdown"
                    break

                try:
                    message = control_queue.get(timeout=0.5)
                    requested_action = parse_control_message(message)
                    if requested_action:
                        action = requested_action
                        print(f"Supervisor received control action: {requested_action}")
                        break
                except queue.Empty:
                    pass
                except Exception as exc:
                    print(f"Control queue read failed: {exc}")

                if not web_process.is_alive():
                    print("Web server process ended unexpectedly.")
                    action = "shutdown"
                    break

                if not bot_process.is_alive():
                    bot_process.join(timeout=0)
                    exitcode = bot_process.exitcode

                    if exitcode == 0:
                        if not bot_exit_notified:
                            print(
                                "Bot process is not running (likely missing token). "
                                "Web dashboard remains available."
                            )
                            bot_exit_notified = True
                    else:
                        print(f"Bot process crashed with exit code {exitcode}. Restarting bot process...")
                        bot_process = multiprocessing.Process(
                            target=run_bot,
                            name="BotProcess",
                            args=(shared_state, command_queue),
                        )
                        bot_process.start()
                        print(f"Bot process restarted (PID: {bot_process.pid})")
                        bot_exit_notified = False

            stop_process(web_process, "WebProcess")
            stop_process(bot_process, "BotProcess")
            close_queue(command_queue, "command queue")
            close_queue(control_queue, "control queue")

        if action == "restart" and not shutdown_requested["value"]:
            print("Restarting application services...")
            time.sleep(0.2)
            continue

        print("Application shut down gracefully.")
        sys.exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
