import asyncio
import multiprocessing
import os
import sys
import time
import signal
import uvicorn
import socket
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def is_port_in_use(port):
    """Checks if a port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(('localhost', port)) == 0

def run_web():
    """Runs the FastAPI web server."""
    sys.path.append(os.getcwd())
    # Log level warning to suppress access logs
    uvicorn.run("web.app:app", host="0.0.0.0", port=7734, reload=False, log_level="warning")

def run_bot():
    """Runs the Discord Bot."""
    sys.path.append(os.getcwd())
    load_dotenv(override=True)
    token = os.getenv("DISCORD_TOKEN")
    
    if not token:
        print("No token found. Bot will not start until token is configured in Web UI.")
        return

    try:
        from bot_service import main
        asyncio.run(main())
    except Exception as e:
        print(f"Bot process failed: {e}")

def main():
    print("--- TheRatBot ---")
    
    # 1. Check Port 7734
    if is_port_in_use(7734):
        print("\nCRITICAL ERROR: Port 7734 is already in use.")
        print("Please stop the other application using this port and try again.")
        sys.exit(1)

    print("Starting services...")
    
    # Start Web Server
    web_process = multiprocessing.Process(target=run_web, name="WebProcess")
    web_process.start()
    print(f"Web Server started on http://localhost:7734 (PID: {web_process.pid})")
    
    # Start Bot
    bot_process = multiprocessing.Process(target=run_bot, name="BotProcess")
    bot_process.start()
    print(f"Bot process started (PID: {bot_process.pid})")
    
    def signal_handler(sig, frame):
        print("\nShutting down...")
        web_process.terminate()
        bot_process.terminate()
        web_process.join()
        bot_process.join()
        print("Services stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Monitor loop
    restart = False
    shutdown = False
    try:
        while True:
            if not web_process.is_alive():
                print("Web server process ended.")
                bot_process.terminate()
                
                # Check for restart flag
                if os.path.exists(".restart"):
                    print("Restarting application...")
                    try:
                        os.remove(".restart")
                    except:
                        pass
                    restart = True
                # Check for shutdown flag
                elif os.path.exists(".shutdown"):
                    print("Shutdown requested...")
                    try:
                        os.remove(".shutdown")
                    except:
                        pass
                    shutdown = True
                break
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)
        
    # Cleanup
    web_process.terminate()
    bot_process.terminate()
    web_process.join()
    bot_process.join()
    
    if restart:
        # Re-run main
        main()
    else:
        if shutdown:
            print("Application shut down gracefully.")
        else:
            print("Services stopped.")
        sys.exit(0)

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
