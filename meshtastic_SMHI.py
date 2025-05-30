#!/usr/bin/env python3

from time import sleep
from datetime import datetime
import requests
import subprocess
import logging
import argparse

_LOGGER = logging.getLogger(__name__)

def truncate_utf8(s, max_bytes=200):

    # If our string is already shorter than 200 bytes, do nothing 
    if len(s.encode('utf-8')) <= max_bytes: 
        return [s]

    # Worst case suffix length
    suffix_length = len(f" {MAX_MESSAGES}/{MAX_MESSAGES}".encode('utf-8')) 
    extra_suffix_length = 0

    words = s.split(" ")
    chunks = []
    current_words = []
    current_size = 0  # track byte length in UTF-8

    for word in words:
        word_bytes = word.encode('utf-8')
        word_size = len(word_bytes)

        # If the word alone is bigger than max_bytes, skip it. This is extremely unlikely.
        if word_size > max_bytes:
            continue
        extra = 1 + word_size

        if len(chunks) == MAX_MESSAGES-1:
            extra_suffix_length = 6

        # If we add " " + word, measure extra bytes
        # (space is 1 byte in UTF-8, plus the new word's bytes)

        if current_size + extra <= max_bytes-suffix_length-extra_suffix_length:
            # Fits in current chunk
            current_words.append(word)
            current_size += extra
        else:
            # Finalize current chunk
            chunks.append(" ".join(current_words))


            if len(chunks) == MAX_MESSAGES:
                chunks[-1] += " [...]"
                # Start a new chunk with the current word
                current_words = []
                current_size = 0

                break

            # Start a new chunk with the current word
            current_words = [word]
            current_size = word_size

    # Finalize any remaining words in the last chunk
    if current_words:
        chunks.append(" ".join(current_words))

    for i in range(len(chunks)):
        chunks[i] = chunks[i].strip() + f" {i+1}/{len(chunks)}"

    return chunks

def fetch_alerts():
    """
    Fetch alerts from the SMHI API.
    """
    _LOGGER.debug("Fetching %s", API_URL)
    try:
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status()  # Raise an HTTPError if the response was unsuccessful
    except requests.RequestException as e:
        _LOGGER.error("Error fetching alerts: ", e)
        return set()

    data = response.json()
    
    alert_ids = set()
    filtered_alerts = []
    
    for alert in data:
        alert_id = alert["id"]
        for wa in alert["warningAreas"]:
            
            # Filter out MESSAGEs
            if wa["warningLevel"]["code"] == "MESSAGE":
                continue

            # Check if warningArea affects area with id=GEOCODE
            if any(a["id"] == GEOCODE for a in wa["affectedAreas"]) :
                # Build the concatenated string "alertIDwarningAreaID"
                combined_id = f"{alert_id}{wa['id']}"

                # Make a shallow copy of the warningArea dict without the 'area' key
                wa_copy = dict(wa)
                wa_copy.pop("area", None)  # remove the area object if present since it is useless and very large
                wa_copy['id'] = combined_id
                alert_ids.add(combined_id)
                filtered_alerts.append(wa_copy)

    return alert_ids, filtered_alerts


def call_meshtastic(template, message, output=True):
    """
    Call meshtastic to send message
    """
    meshtastic_cmd = template.copy()
    meshtastic_cmd.append(message)

    try:
        if not DRY_RUN:
            result = subprocess.run(meshtastic_cmd, capture_output = True, text = True, check = True)
            stdout = result.stdout
        else:
            stdout = "DRY RUN: "+message
            
        if output:
            _LOGGER.info(stdout.strip())
        return stdout
    except subprocess.CalledProcessError as e:
        _LOGGER.error("Error running meshtastic command: %s", e)
        return False


def main():
    """
    Main loop:
    1. Fetches alerts
    2. Checks for new alerts vs. previously seen,
    3. Parses data and generates a message
    4. Sends message to meshtastic
    5. Waits before repeating.
    """
    first = not DRY_RUN

    # This is how we handle message queueing.
    # This list contains new lists, each of which will contain messages. Every iteration, first list is .pop() and a new on appended. 
    message_queue = [[] for i in range(REPEAT_NUM_CYCL*REPEAT_NUM_MSG)]
    known_alerts = set()  # Keep track of the alerts we have seen

    while True:
        if REPEAT_NUM_MSG > 0 and REPEAT_NUM_CYCL > 0:
            # Start by sending any queued messages
            queued_messages = message_queue.pop(0)

            _LOGGER.debug(f"QUEUE: {len(queued_messages)} messages to be sent this iteration")
            
            for message in queued_messages:
                call_meshtastic(MESHTASTIC_CMD_TEMPLATE, message)

            message_queue.append([]) # Create new empty queue slot

        # Fetch new alerts
        current_alerts, data = fetch_alerts()

        # Find what's new compared to known_alerts
        new_alerts = current_alerts - known_alerts

        _LOGGER.info("Got %s alerts in total of which %s were new.", len(current_alerts), len(new_alerts))

        if not first: # Make sure we don't spam the channel when script starts. Assume any alerts that are already present have been sent already

            for id in new_alerts:
                alert = next((d for d in data if d["id"] == id), None)

                message = f"SMHI: {alert['warningLevel']['sv']} varning för {alert['areaName']['sv']} - {alert['eventDescription']['sv']} från {datetime.fromisoformat(alert['approximateStart']).strftime('%Y-%m-%d %H:%M')} till {datetime.fromisoformat(alert['approximateEnd']).strftime('%Y-%m-%d %H:%M')}"
                print(message)
    
                new_messages = truncate_utf8(message)
                _LOGGER.debug(f"Alert was split into {len(new_messages)} messages. Sending now")

                for message in new_messages:
                    call_meshtastic(MESHTASTIC_CMD_TEMPLATE, message)
                
                if REPEAT_NUM_MSG > 0 and REPEAT_NUM_CYCL > 0:
                    # Add new messages to queue
                    for i in range(REPEAT_NUM_MSG):
                        message_queue[(i+1)*REPEAT_NUM_CYCL-1].extend(new_messages)
                        _LOGGER.debug(f"QUEUE: Added {len(new_messages)} messages to queue slot {(i+1)*REPEAT_NUM_CYCL}.")


        # Update our known alerts set
        known_alerts = current_alerts
        if first:
            _LOGGER.debug("Variable 'first' = False")
            first = False


        # Sleep for INTERVAL seconds before checking again
        _LOGGER.debug("Sleeping for %s seconds...", INTERVAL)
        sleep(INTERVAL)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Fetches Swedish weather warnings from SMHI and broadcasts them to your local Meshtastic network.")

    # Required:
    parser.add_argument("executable", type=str, help="Path to meshtastic executable")

    # Optional
    parser.add_argument("--verbose", action="store_true", help="Increase output verbosity. [False]")
    parser.add_argument("--dry-run", action="store_true", help="Suspend calls to meshtastic executable [False]")
    parser.add_argument("--connection-type", type=str, default="host", help="Connection type (host/port/ble) [host]")
    parser.add_argument("--connection-argument", type=str, default="localhost", help="Connection argument [localhost]")
    parser.add_argument("--ch-index", type=str, default="0", help="Meshtastic channel to which messages will be sent. [0]")
    parser.add_argument("--api-uri", type=str, default="https://opendata-download-warnings.smhi.se/ibww/api/version/1/warning.json", help="API URI to fetch [https://opendata-download-warnings.smhi.se/ibww/api/version/1/warning.json]")
    parser.add_argument("--api-interval", type=int, default=120, help="Time interval in seconds at which API will be fetched. [120]")
    parser.add_argument("--api-geocode", type=int, default=1, help="Geocode. [1]")
    parser.add_argument("--max-messages", type=int, default=2, help="Maximum number of messages to send for each alert. Will trunkate to this number of messages. [2]")
    parser.add_argument("--repeat-number", type=int, default=0, help="Number of re-broadcasts to perform. [1]")
    parser.add_argument("--repeat-cycles", type=int, default=2, help="Number of api-intervals between rebroadcast. [2]")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.DEBUG)
    else:
        logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.INFO)
    
    DRY_RUN = args.dry_run
    INTERVAL = args.api_interval
    CHANNEL = args.ch_index
    API_URL = args.api_uri
    GEOCODE = args.api_geocode
    MAX_MESSAGES = args.max_messages
    REPEAT_NUM_MSG = args.repeat_number
    REPEAT_NUM_CYCL = args.repeat_cycles

    MESHTASTIC_CMD_TEMPLATE = [args.executable, "--"+args.connection_type, args.connection_argument, "--ch-index", CHANNEL, "--sendtext"]  # Message will be appended at the end

    _LOGGER.info(f"""Starting meshtastic_VMA\n
Parameters:
    verbose: {args.verbose}
    dry-run: {DRY_RUN}
    executable: {args.executable}
    connection-type: {args.connection_type}
    connection-argument: {args.connection_argument}
    ch-index: {CHANNEL}
    api-uri: {args.api_uri}
    api-interval: {INTERVAL}
    api-geocode: {GEOCODE}
    max-messages: {MAX_MESSAGES}
    repeat-number: {REPEAT_NUM_MSG}
    repeat-cycles: {REPEAT_NUM_CYCL}

    Constructed API_URL: {API_URL}
    Constructed MESHTASTIC_CMD_TEMPLATE: {" ".join(MESHTASTIC_CMD_TEMPLATE)} [message]""")

    # Attempt connecting to radio
    if not DRY_RUN and not call_meshtastic([args.executable], "--info", False):
        raise Exception("Could not communicate with meshtastic device") 
    
    main()