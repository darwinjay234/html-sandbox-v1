from flask import Flask, jsonify
import threading
import logging
import time
from crypto_scanner import evaluate_trading_system, WATCHLIST

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

def run_watchlist_scan():
    """
    Executes the watchlist scan in a separate background thread
    to bypass the 30-second HTTP timeout of cron-job.org.
    """
    logger.info("--- Starting background watchlist scan ---")
    all_results = {}
    
    for coin in WATCHLIST:
        try:
            logger.info(f"Scanning {coin.upper()} in background...")
            result = evaluate_trading_system(coin_id=coin)
            all_results[coin] = result
        except Exception as e:
            logger.error(f"Error scanning {coin}: {e}")
        
        # Pace API requests between coins
        if coin != WATCHLIST[-1]:
            logger.info("Pacing API... sleeping 10s before next coin...")
            time.sleep(10)
            
    logger.info("--- Background watchlist scan complete ---")

@app.route("/scan", methods=["GET", "POST"])
def trigger_scan():
    """
    HTTP endpoint triggered by cron-job.org.
    Spawns a background thread for the scan and returns a response instantly.
    """
    logger.info("HTTP request received: Triggering background scan.")
    
    # Spawn background thread for execution
    thread = threading.Thread(target=run_watchlist_scan)
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "status": "success",
        "message": "Watchlist scan successfully triggered in background."
    }), 202

@app.route("/", methods=["GET"])
def health_check():
    """
    Root endpoint for service health checks.
    """
    return jsonify({
        "service": "Crypto Market Scanner Web Service",
        "status": "online",
        "watchlist": WATCHLIST
    }), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
