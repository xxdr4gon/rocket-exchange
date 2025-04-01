#!/usr/bin/env python3
"""
Exchange Calendar to RocketChat Status Sync

This script:
1. Connects to Exchange using NTLM authentication and a service account with calendar reading permissions
2. Retrieves all users from RocketChat (excluding those with msteams.alias suffix)
3. Checks each user's current calendar status in Exchange asynchronously
4. Respects manually set statuses in RocketChat
5. Supports Pexip call status integration
6. Updates RocketChat status based on priority of status sources

Requirements:
- exchangelib (pip install exchangelib)
- requests (pip install requests)
- python-dotenv (pip install python-dotenv)
- flask (pip install flask) - for Pexip webhook endpoint
- aiohttp (pip install aiohttp)
- asyncio
"""

import pytz
import os
import datetime
import time
import logging
import json
import asyncio
import concurrent.futures
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv
import requests
from exchangelib import Credentials, Account, Configuration, NTLM, DELEGATE, EWSTimeZone, EWSDateTime
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
from exchangelib.errors import ErrorAccessDenied, ErrorFolderNotFound, ErrorInvalidPropertyRequest

# Optional: For webhook server
from flask import Flask, request, jsonify
import threading

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="/var/log/rocketchat_calendar_sync.log",
)
logger = logging.getLogger("rocketchat-exchangeconnector")

# Load environment variables
load_dotenv()

# Exchange configuration
EXCHANGE_USERNAME = os.getenv("EXCHANGE_USERNAME", "default")
EXCHANGE_PASSWORD = os.getenv("EXCHANGE_PASSWORD", "default")
EXCHANGE_SERVER = os.getenv("EXCHANGE_SERVER", "default")
EXCHANGE_DOMAIN = "SET MANUALLY IF NEEDED"
VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() == "true"

# RocketChat configuration
RC_API_URL = os.getenv("RC_API_URL", "http://rocket.chat.instance/api/v1")
RC_ADMIN_USERNAME = os.getenv("RC_ADMIN_USERNAME")
RC_ADMIN_PASSWORD = os.getenv("RC_ADMIN_PASSWORD")
RC_AUTH_TOKEN = os.getenv("RC_AUTH_TOKEN")
RC_USER_ID = os.getenv("RC_USER_ID")

# Status override timeframe - don't overwrite manually set statuses within this period (in seconds)
MANUAL_STATUS_RESPECT_TIME = int(os.getenv("MANUAL_STATUS_RESPECT_TIME", "360"))  # Default 1 hour

# Maximum concurrent calendar checks
MAX_CONCURRENT_CALENDAR_CHECKS = int(os.getenv("MAX_CONCURRENT_CALENDAR_CHECKS", "50"))

# Pexip call status storage
PEXIP_CALL_STATUS = {}  # Format: {email: {"status": "busy", "title": "In a Pexip call", "timestamp": time.time()}}

# Mapping of user statuses set by this script
SCRIPT_SET_STATUS = {}  # Format: {user_id: {"status": "busy", "text": "In a meeting", "timestamp": time.time()}}

CALL_ATTRIBUTES = {}

class CalendarSync:
    def __init__(self):
        self.rc_token = None
        self.rc_user_id = None
        self.rc_headers = None
        self.exchange_credentials = None
        self.exchange_config = None
        self.service_account = None
        self.user_status_cache = {}  # Store when users manually set their status
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALENDAR_CHECKS)

    def authenticate_rocketchat(self) -> bool:
        """Authenticate with RocketChat API"""
        if RC_AUTH_TOKEN and RC_USER_ID:
            self.rc_token = RC_AUTH_TOKEN
            self.rc_user_id = RC_USER_ID
            self.rc_headers = {
                "X-Auth-Token": self.rc_token,
                "X-User-Id": self.rc_user_id,
                "Content-Type": "application/json",
            }
            return True

        try:
            response = requests.post(
                f"{RC_API_URL}/login",
                json={"username": RC_ADMIN_USERNAME, "password": RC_ADMIN_PASSWORD},
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.rc_token = data["data"]["authToken"]
                self.rc_user_id = data["data"]["userId"]
                self.rc_headers = {
                    "X-Auth-Token": self.rc_token,
                    "X-User-Id": self.rc_user_id,
                    "Content-Type": "application/json",
                }
                return True
            else:
                logger.error("RocketChat authentication failed")
                return False

        except Exception as e:
            logger.error(f"Error authenticating with RocketChat: {str(e)}")
            return False

    def get_pexip_call_status(self, email: str) -> Tuple[str, str]:
        """Check if user is in a Pexip call"""
        if email in PEXIP_CALL_STATUS:
            call_info = PEXIP_CALL_STATUS[email]
            # Check if the call status is still recent (within last 5 minutes)
            if time.time() - call_info.get("timestamp", 0) < 300:  # 5 minutes
                return call_info.get("status", "Free"), call_info.get("title", "")

        return "Free", ""

    def setup_exchange_connection(self) -> bool:
        """Set up Exchange connection using service account and NTLM authentication"""
        try:
            logger.info("Attempting to connect to Exchange server...")
            print("Connecting to Exchange server...")

            if not VERIFY_SSL:
                BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
                logger.info("SSL verification disabled")

            username = f"{EXCHANGE_USERNAME}"
            self.exchange_credentials = Credentials(username=username, password=EXCHANGE_PASSWORD)
            logger.info(f"Created credentials for {username}")

            self.exchange_config = Configuration(
                server=EXCHANGE_SERVER, credentials=self.exchange_credentials, auth_type=NTLM
            )
            logger.info(f"Created configuration for server {EXCHANGE_SERVER} using NTLM auth")

            self.service_account = Account(
                primary_smtp_address=f"{EXCHANGE_USERNAME}@mfa.ee",
                config=self.exchange_config,
                autodiscover=True,
                access_type=DELEGATE,
            )

            logger.info(f"SUCCESS: Connected to Exchange as {username}")
            print(f"Successfully connected to Exchange as {username}")
            return True
        except Exception as e:
            logger.error(f"EXCHANGE ERROR: Failed to connect to Exchange: {str(e)}")
            print(f"Exchange connection failed: {str(e)}")
            return False

    def get_rocketchat_users(self) -> List[Dict]:
        """Fetch all RocketChat users, handling pagination."""
        try:
            users = []
            offset = 0
            limit = 200  # Adjust if needed

            while True:
                response = requests.get(
                    f"{RC_API_URL}/users.list",
                    headers=self.rc_headers,
                    params={"count": limit, "offset": offset},
                )
                response.raise_for_status()
                data = response.json()

                fetched_users = data.get("users", [])
                if not fetched_users:
                    break  # No more users to fetch

                users.extend(
                    [
                        {
                            "id": user["_id"],
                            "username": user["username"],
                            "name": user.get("name", user["username"]),
                            "email": user["emails"][0]["address"],
                            "status": user.get("status", ""),
                            "statusText": user.get("statusText", "")
                        }
                        for user in fetched_users
                        if user.get("emails")
                        and len(user["emails"]) > 0
                        and not user.get("username", "").endswith("msteams.alias")
                    ]
                )

                offset += limit

            logger.info(f"Found {len(users)} RocketChat users (excluding msteams.alias)")
            return users

        except Exception as e:
            logger.error(f"Error getting RocketChat users: {str(e)}")
            return []

    def get_rocketchat_user_status_history(self, user_id: str) -> Dict:
        """Get a user's status change history to determine if they manually set it"""
        try:
            response = requests.get(
                f"{RC_API_URL}/users.getStatus",
                headers=self.rc_headers,
                params={"userId": user_id},
            )
            response.raise_for_status()
            data = response.json()

            return {
                "status": data.get("status", ""),
                "statusText": data.get("statusText", ""),
                "lastChanged": data.get("statusConnection", {}).get("updatedAt", 0)
            }
        except Exception as e:
            logger.error(f"Error getting status history for user {user_id}: {str(e)}")
            return {}

    def is_manually_set_status(self, user_id: str, current_status: Dict) -> bool:
        """
        Determine if a user's status was manually set

        This works by tracking whether our script set the current status. If the
        current status doesn't match what we last set, it was manually changed.
        """
        current_status_text = current_status.get("statusText", "")
        current_status_type = current_status.get("status", "")

        # If this user has no status record in our tracking, treat as manual
        if user_id not in SCRIPT_SET_STATUS:
            # But only if it's not the default "online" with empty status text
            if current_status_type != "online" or current_status_text != "":
                logger.info(f"User {user_id} has a non-default status we didn't set - respecting as manual")
                return True
            return False

        # Get what we previously set for this user
        last_set = SCRIPT_SET_STATUS[user_id]
        last_set_status = last_set.get("status", "")
        last_set_text = last_set.get("text", "")

        # Check manual status timeout - don't consider anything we set ourselves as manual
        timeout = time.time() - MANUAL_STATUS_RESPECT_TIME

        # If current status doesn't match what we last set, determine if it was manually changed
        if current_status_type != last_set_status or current_status_text != last_set_text:
            # If we set "busy: In a meeting" before, and the current status is just "busy"
            # it's likely an internal state mismatch and not a manual change
            if (
                last_set_status == "busy" and
                current_status_type == "busy" and
                last_set_text.startswith("In a meeting") and
                (not current_status_text or current_status_text == "busy")
            ):
                logger.info(f"User {user_id} has internal state mismatch, not treating as manual")
                return False

            logger.info(f"User {user_id} changed status from our set '{last_set_status}: {last_set_text}' to '{current_status_type}: {current_status_text}' - respecting as manual")
            return True

        return False

    def update_rocketchat_status(self, user_id: str, status: str, message: str = "") -> bool:
        """
        Update a user's RocketChat status and set a custom message
        Also track that we set this status
        """
        try:
            rc_status_map = {"online": "online", "busy": "busy", "away": "away"}
            rc_status = rc_status_map.get(status.lower(), "online")

            payload = {"userId": user_id, "status": rc_status, "message": message}
            response = requests.post(f"{RC_API_URL}/users.setStatus", headers=self.rc_headers, json=payload)

            response_json = response.json()
            logger.info(f"RocketChat API response for {user_id}: {response_json}")

            # Track that we set this status
            if response_json.get("success", False):
                SCRIPT_SET_STATUS[user_id] = {
                    "status": rc_status,
                    "text": message,
                    "timestamp": time.time()
                }

            response.raise_for_status()
            return response_json.get("success", False)

        except requests.exceptions.RequestException as e:
            logger.error(f"Error updating RocketChat status for user {user_id}: {str(e)}")
            return False

    def get_current_calendar_status(self, email: str) -> Tuple[str, str]:
        """Get current free/busy status and meeting title using LimitedDetails access"""
        try:
            logger.info(f"Checking calendar status for {email}")

            local_tz = pytz.timezone("Europe/Tallinn")
            now = datetime.datetime.now(pytz.utc).astimezone(local_tz)

            # Time window to check (a 10-minute window centered on now)
            start_time = now - datetime.timedelta(minutes=5)
            end_time = now + datetime.timedelta(minutes=5)

            start_ews = EWSDateTime.from_datetime(start_time)
            end_ews = EWSDateTime.from_datetime(end_time)

            # Get free/busy status first
            is_busy = self._check_free_busy(email, start_ews, end_ews)

            if not is_busy:
                logger.info(f"User {email} is free according to calendar")
                return 'Free', ""

            # Try to get meeting title - first try with direct access
            meeting_title = self._try_get_meeting_title(email, start_ews, end_ews)

            # Return busy status with meeting title if available
            return "Busy", "In a meeting" + (f": {meeting_title}" if meeting_title else "")

        except Exception as e:
            logger.error(f"CALENDAR ERROR: Failed to get calendar status for {email}: {str(e)}")
            print(f"Calendar status check failed for {email}: {str(e)}")
            # It's safer to return Free in case of errors
            return 'Free', ""

    def _check_free_busy(self, email: str, start_ews: EWSDateTime, end_ews: EWSDateTime) -> bool:
        """Check if a user is busy during the specified time period"""
        try:
            logger.info(f"Requesting free/busy for {email}")

            # Create a schedule request to check free/busy status
            result = self.service_account.protocol.get_free_busy_info(
                accounts=[(email, 'Required', False)],
                start=start_ews,
                end=end_ews,
                merged_free_busy_interval=6  # Shorter interval (6 minutes)
            )

            # Process the result
            for item in result:
                # Handle FreeBusyView object directly
                if hasattr(item, 'merged'):
                    merged_status = item.merged
                    if merged_status and any(c != '0' for c in merged_status):
                        logger.info(f"User {email} is busy according to calendar (status code: {merged_status})")
                        return True

                # Handle tuple format (mailbox, data)
                elif isinstance(item, tuple) and len(item) >= 2:
                    mailbox, data = item[:2]

                    # Check merged status in the data object
                    if hasattr(data, 'merged'):
                        merged_status = data.merged
                        if merged_status and any(c != '0' for c in merged_status):
                            logger.info(f"User {email} is busy according to calendar (status code: {merged_status})")
                            return True

                    # Check merged_free_busy attribute (old format)
                    elif hasattr(data, 'merged_free_busy'):
                        free_busy_status = data.merged_free_busy
                        if free_busy_status != 'Free':
                            logger.info(f"User {email} is busy according to calendar")
                            return True

            return False

        except Exception as e:
            logger.error(f"Error checking free/busy for {email}: {str(e)}")
            return False

    def _try_get_meeting_title(self, email: str, start_ews: EWSDateTime, end_ews: EWSDateTime) -> str:
        """Try to get the meeting title for a user's current meeting"""
        try:
            # Try to access the user's calendar directly
            target_account = Account(
                primary_smtp_address=email,
                config=self.exchange_config,
                autodiscover=False,
                access_type=DELEGATE,
            )

            # Get calendar folder
            calendar = target_account.calendar

            # Find current meetings
            current_meetings = calendar.filter(
                start__lt=end_ews,
                end__gt=start_ews,
            )

            # Get the first meeting's subject if available
            if current_meetings:
                for meeting in current_meetings:
                    try:
                        meeting_title = meeting.subject
                        if meeting_title:
                            logger.info(f"Found meeting title for {email}: {meeting_title}")
                            return meeting_title
                    except ErrorInvalidPropertyRequest:
                        # This happens when we have limited access
                        logger.info(f"Limited access to meeting details for {email}")
                        continue

            # If we couldn't get direct meeting title, try an alternative approach
            # using free/busy information that might include subjects
            try:
                view_options = {'requested_view': 'DetailedMerged'}
                result = self.service_account.protocol.get_free_busy_info(
                    accounts=[(email, 'Required', False)],
                    start=start_ews,
                    end=end_ews,
                    merged_free_busy_interval=6,  # Shorter interval (6 minutes)
                    **view_options
                )

                # Try to extract meeting subject from calendar event data if available
                for item in result:
                    if isinstance(item, tuple) and len(item) >= 2:
                        _, data = item[:2]
                        if hasattr(data, 'calendar_events') and data.calendar_events:
                            for event in data.calendar_events:
                                if hasattr(event, 'subject') and event.subject:
                                    logger.info(f"Found meeting subject from free/busy data for {email}: {event.subject}")
                                    return event.subject
            except Exception as e:
                logger.info(f"Could not get detailed free/busy info for {email}: {str(e)}")

            return ""

        except (ErrorAccessDenied, ErrorFolderNotFound) as e:
            # Expected - if we can't access calendar details, we fall back to free/busy only
            logger.info(f"Could not access detailed calendar for {email}: {str(e)}")
            return ""
        except Exception as e:
            logger.warning(f"Error getting meeting title for {email}: {str(e)}")
            return ""

    async def process_user(self, user):
        """Process a single user's status asynchronously"""
        try:
            # Check if the user has manually set their status
            current_status = {"status": user.get("status", ""), "statusText": user.get("statusText", "")}

            # Skip users with manually set status (but not our own statuses)
            if self.is_manually_set_status(user["id"], current_status):
                logger.info(f"Respecting manually set status for {user['username']}")
                return

            # Check Pexip call status first (highest priority)
            pexip_status, pexip_title = self.get_pexip_call_status(user["email"])

            if pexip_status != "Free":
                # User is in a Pexip call, set to busy
                success = self.update_rocketchat_status(user["id"], pexip_status, pexip_title)
                logger.info(f"Updated {user['username']} ({user['email']}) to {pexip_status} (Pexip call): {'Success' if success else 'Failed'}")
                return

            # Run calendar check in the thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            calendar_status, meeting_title = await loop.run_in_executor(
                self.executor,
                self.get_current_calendar_status,
                user["email"]
            )

            if calendar_status != "Free":
                success = self.update_rocketchat_status(user["id"], calendar_status, meeting_title)
                logger.info(f"Updated {user['username']} ({user['email']}) to {calendar_status} with title '{meeting_title}': {'Success' if success else 'Failed'}")
                return

            # If no other status, set to online
            # But don't update unnecessarily if already online with no status text
            if current_status.get("status") != "online" or current_status.get("statusText") != "":
                success = self.update_rocketchat_status(user["id"], "online", "")
                logger.info(f"Reset {user['username']} ({user['email']}) to online: {'Success' if success else 'Failed'}")

        except Exception as e:
            logger.error(f"Error processing user {user.get('username', 'unknown')}: {str(e)}")

    async def sync_all_users_async(self):
        """Sync all users' calendar status to RocketChat asynchronously"""
        if not self.authenticate_rocketchat():
            logger.error("Failed to authenticate with RocketChat. Exiting.")
            return

        if not self.setup_exchange_connection():
            logger.error("Failed to set up Exchange connection. Exiting.")
            return

        users = self.get_rocketchat_users()
        if not users:
            logger.warning("No users found. Exiting.")
            return

        # Process users concurrently
        tasks = [self.process_user(user) for user in users]
        await asyncio.gather(*tasks)

    def sync_all_users(self):
        """Wrapper for async sync to be called from synchronous code"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.sync_all_users_async())
        finally:
            loop.close()


# Flask application for Pexip webhook endpoint
app = Flask(__name__)

@app.route('/pexip-webhook', methods=['POST'])
def pexip_webhook():
    """Webhook endpoint for Pexip call events"""
    try:
        logger.info("WEBHOOK RECEIVED: Pexip webhook triggered")
        data = request.json
        logger.info(f"Webhook payload: {json.dumps(data)}")

        event_type = data.get('event')
        participant = data.get('participant', {})
        email = participant.get('email')

        logger.info(f"Webhook details: Event={event_type}, Email={email}")

        if not email:
            logger.warning("No email provided in webhook data")
            return jsonify({"status": "error", "message": "No email provided"}), 400

        if event_type == 'participant_connected':
            # User joined a call
            PEXIP_CALL_STATUS[email] = {
                "status": "busy",
                "title": "In a Pexip call",
                "timestamp": time.time()
            }
            logger.info(f"CALL JOINED: User {email} joined a Pexip call")
            print(f"User {email} joined a Pexip call")

        elif event_type == 'participant_disconnected':
            # User left a call
            if email in PEXIP_CALL_STATUS:
                del PEXIP_CALL_STATUS[email]
                logger.info(f"CALL ENDED: User {email} left a Pexip call")
                print(f"User {email} left a Pexip call")

        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"WEBHOOK ERROR: Error processing Pexip webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Add a test endpoint to verify the webhook server is running
@app.route('/test', methods=['GET'])
def test_endpoint():
    logger.info("Test endpoint hit")
    return jsonify({"status": "success", "message": "Webhook server is running"}), 200

@app.route('/participant-policy', methods=['POST'])
def participant_policy():
    data = request.json
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    call_uuid = data.get("call_uuid")
    idp_attributes = data.get("idp_attributes", {})

    if not call_uuid:
        return jsonify({"error": "Missing call_uuid"}), 400

    # Store relevant attributes (e.g., email)
    email = idp_attributes.get("emailaddress")  # Assuming email is in 'emailaddress'

    if email:
        CALL_ATTRIBUTES[call_uuid] = {
            "email": email,
            "timestamp": time.time()  # Add a timestamp to handle stale entries
        }
        logging.info(f"Stored attributes for call_uuid {call_uuid}: {email}")
    else:
        logging.warning(f"No email address found in idp_attributes for call_uuid {call_uuid}")

    # Always respond with "continue"
    response = {"action": "continue", "result": {}}
    return jsonify(response), 200


@app.route('/pexip-events', methods=['POST'])
def pexip_events():
    data = request.json
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    try:
        event_type = data.get("type")

        if event_type == "participant_created":
            call_uuid = data.get("call_uuid")
            participant_id = data.get("id")  # Pexip Participant ID

            if not call_uuid or not participant_id:
                return jsonify({"error": "Missing call_uuid or participant_id"}), 400

            # Retrieve stored attributes
            if call_uuid in CALL_ATTRIBUTES:
                email = CALL_ATTRIBUTES[call_uuid]["email"]

                # Update PEXIP_CALL_STATUS based on the email
                PEXIP_CALL_STATUS[email] = {
                    "status": "busy",
                    "title": "In a Pexip call",
                    "timestamp": time.time()
                }
                logging.info(f"Participant created: call_uuid={call_uuid}, email={email}")

                # Optionally, clean up stale entries after a delay (e.g., 10 minutes)
                def cleanup_attributes(uuid):
                    time.sleep(600)  # Wait 10 minutes
                    if uuid in CALL_ATTRIBUTES:
                        del CALL_ATTRIBUTES[uuid]
                        logging.info(f"Cleaned up attributes for call_uuid {uuid}")

                threading.Thread(target=cleanup_attributes, args=(call_uuid,)).start()

            else:
                logging.warning(f"No attributes found for call_uuid {call_uuid}")

        elif event_type == "participant_updated":
            participant_id = data.get("id")  # Pexip Participant ID
            # You might need to handle participant updates, e.g., when a participant leaves
            logging.info(f"Participant updated event, ID: {participant_id}")
        elif event_type == "call_ended":
            call_uuid = data.get("call_uuid")
            # Remove all active call participants from PEXIP_CALL_STATUS dict
            users_to_remove = [email for email, call in PEXIP_CALL_STATUS.items() if call["title"] == "In a Pexip call" and call_uuid in CALL_ATTRIBUTES and CALL_ATTRIBUTES[call_uuid]["email"] == email]
            for user_email in users_to_remove:
                del PEXIP_CALL_STATUS[user_email]
            # Cleanup the entry from temporary CALL_ATTRIBUTES store
            if call_uuid in CALL_ATTRIBUTES:
              del CALL_ATTRIBUTES[call_uuid]
        else:
            logging.info(f"Received event type: {event_type}")

        return jsonify({"success": True}), 200

    except Exception as e:
        logging.error(f"Error processing Pexip event: {str(e)}")
        return jsonify({"error": str(e)}), 500


def run_flask_app():
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=5000, debug=False)).start()

def start_webhook_server():
    """Start the Flask webhook server in a separate thread"""
    webhook_port = int(os.getenv("WEBHOOK_PORT", "5000"))
    webhook_host = os.getenv("WEBHOOK_HOST", "0.0.0.0")

    logger.info(f"Setting up Pexip webhook server on {webhook_host}:{webhook_port}")
    print(f"\n*** WEBHOOK SERVER STARTING AT http://{webhook_host}:{webhook_port}/pexip-webhook ***\n")

    # Add logging handlers to Flask app
    for handler in logger.handlers:
        app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

    threading.Thread(
        target=app.run,
        kwargs={"host": webhook_host, "port": webhook_port, "debug": False, "use_reloader": False},
        daemon=True
    ).start()

    # Give the server a moment to start
    time.sleep(1)
    logger.info(f"Pexip webhook server started on {webhook_host}:{webhook_port}")
    print(f"Pexip webhook server running at http://{webhook_host}:{webhook_port}/pexip-webhook")

if __name__ == "__main__":
    try:
        print("\n*** STARTING ROCKETCHAT-EXCHANGE SYNC SERVICE ***\n")

        # Start webhook server if ENABLE_WEBHOOK is set to true
        if os.getenv("ENABLE_WEBHOOK", "true").lower() == "true":
            start_webhook_server()

        sync = CalendarSync()
        print("\nPerforming initial user sync...")
        sync.sync_all_users()
        print("\nInitial sync completed")

        # Keep the script running with better user feedback
        print("\n*** SERVICE IS RUNNING ***")
        print("Press Ctrl+C to stop the service")

        sync_interval = int(os.getenv("SYNC_INTERVAL", "20"))  # Default 1 minute
        print(f"Will sync users every {sync_interval} seconds\n")

        try:
            while True:
                time.sleep(sync_interval)
                print(f"\nPerforming scheduled sync at {datetime.datetime.now().strftime('%H:%M:%S')}")
                sync.sync_all_users()
                print("Sync completed")
        except KeyboardInterrupt:
            print("\nService stopped by user (Ctrl+C)")
            logger.info("Service stopped by user")

    except Exception as e:
        logger.critical(f"CRITICAL ERROR: {str(e)}")
        print(f"\nCRITICAL ERROR: {str(e)}")
