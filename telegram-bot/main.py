from datetime import datetime, timezone
import os
from firebase_admin import firestore
from google.cloud.firestore_v1.base_document import DocumentSnapshot
from google.cloud.firestore_v1.base_query import FieldFilter
import json
from typing import List, Optional, Self
import pytz
import telebot
import httpx
import pyproj
from pydantic import BaseModel, field_validator
import csv
from io import StringIO
from flask import Flask, jsonify
from google.cloud.logging import (  # pylint: disable=ungrouped-imports
    Client as GCloudLoggingClient,
)
from google.cloud.logging.handlers import CloudLoggingHandler
import logging

LOG_NAME = "ar-baak-taxi-tg-bot"
gcloud_logging_client = GCloudLoggingClient()
gcloud_logging_handler = CloudLoggingHandler(gcloud_logging_client, name=LOG_NAME)

logger = logging.getLogger(LOG_NAME)
logger.setLevel(logging.DEBUG)
logger.addHandler(gcloud_logging_handler)


flask_logger = logging.getLogger("werkzeug")
flask_logger.setLevel(logging.DEBUG)
flask_logger.addHandler(gcloud_logging_handler)

# Initialize Flask app
app = Flask(__name__)

# Initialize Firestore
DB_NAME = f"taxi-{os.environ.get('ENV', 'dev')}"
db = firestore.Client(database=DB_NAME)

USER_COLLECTION_NAME = "users"
TRIP_COLLECTION_NAME = "trips"
SHIFT_COLLECTION_NAME = "shifts"


# Telegram bot setup
BOT_TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

# Set bot commands
commands = [
    telebot.types.BotCommand("/start_shift", "開工"),
    telebot.types.BotCommand("/end_shift", "收工"),
    telebot.types.BotCommand("/get_trips", "睇返之前嘅記錄"),
]
bot.set_my_commands(commands)

# Coordinate transformer
transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2326")


class Trip(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    trip_id: Optional[str] = None
    shift_id: Optional[str] = None
    user_id: str
    start_latitude: Optional[float] = None
    start_longitude: Optional[float] = None
    start_address: str
    start_time: datetime
    end_latitude: Optional[float] = None
    end_longitude: Optional[float] = None
    end_address: Optional[str] = None
    end_time: Optional[datetime] = None
    fare: Optional[float] = None

    @field_validator("fare")
    def validate_fare(cls, value: float):
        """Validates that the fare is a positive number."""
        if value is not None and value <= 0:
            raise ValueError("Fare must be a positive number.")
        return value

    def to_firestore_dict(self):
        """Converts the Trip object to a dictionary suitable for Firestore."""
        data = self.model_dump(exclude_unset=True)
        return data

    @classmethod
    def from_firestore_doc(cls, doc: DocumentSnapshot) -> Optional[Self]:
        """Creates a Trip object from a Firestore document snapshot."""
        if doc.exists:
            return cls.model_validate(doc.to_dict())
        return None

    def save_to_firestore(self):
        """Saves or updates the Trip object in Firestore."""
        trip_ref = db.collection(TRIP_COLLECTION_NAME).document()
        self.trip_id = trip_ref.id
        trip_ref.set(self.to_firestore_dict())

    @classmethod
    def get_trip_by_id(cls, trip_id: str) -> Optional[Self]:
        """Gets a Trip object from Firestore by its ID."""
        trip_ref = db.collection(TRIP_COLLECTION_NAME).document(trip_id)
        trip_doc = trip_ref.get()
        return cls.from_firestore_doc(trip_doc)

    def update_in_firestore(self):
        """Updates the corresponding Firestore document with the current User data."""
        trip_ref = db.collection(TRIP_COLLECTION_NAME).document(str(self.trip_id))
        trip_ref.update(self.model_dump(exclude_unset=True))


class Shift(BaseModel):
    shift_id: Optional[str] = None
    user_id: str
    start_time: datetime
    end_time: Optional[datetime] = None
    total_trips: int = 0
    total_fare: float = 0.0

    def to_firestore_dict(self):
        """Converts the Shift object to a dictionary suitable for Firestore."""
        data = self.model_dump(exclude_unset=True)
        if data.get("start_time"):
            data["start_time"] = firestore.SERVER_TIMESTAMP
        if data.get("end_time"):
            data["end_time"] = firestore.SERVER_TIMESTAMP
        return data

    @classmethod
    def from_firestore_doc(cls, doc: DocumentSnapshot) -> Optional[Self]:
        """Creates a Shift object from a Firestore document snapshot."""
        if doc.exists:
            return cls.model_validate(doc.to_dict())
        return None

    @classmethod
    def get_shift_by_id(cls, shift_id: str) -> Optional[Self]:
        """Gets a Shift object from Firestore by its ID."""
        shift_ref = db.collection(SHIFT_COLLECTION_NAME).document(shift_id)
        shift_doc = shift_ref.get()
        return cls.from_firestore_doc(shift_doc)

    def save_to_firestore(self):
        """Saves or updates the Shift object in Firestore."""
        shift_ref = db.collection(SHIFT_COLLECTION_NAME).document()
        self.shift_id = shift_ref.id
        shift_ref.set(self.to_firestore_dict())

    def update_in_firestore(self):
        """Updates the corresponding Firestore document with the current Shift data."""
        shift_ref = db.collection(SHIFT_COLLECTION_NAME).document(str(self.shift_id))
        shift_ref.update(self.model_dump(exclude_unset=True))

    def get_all_trips(self) -> List[Trip]:
        """Retrieves all trips associated with this user from Firestore."""
        trips_ref = (
            db.collection(TRIP_COLLECTION_NAME)
            .where(filter=FieldFilter("shift_id", "==", str(self.shift_id)))
            .stream()
        )
        trips = [Trip.from_firestore_doc(trip_doc) for trip_doc in trips_ref]
        return [_ for _ in trips if _ is not None]


class User(BaseModel):
    user_id: int
    first_name: str
    last_name: Optional[str]
    username: Optional[str]
    active_trip: Optional[str] = None
    active_shift: Optional[str] = None
    total_trips: int = 0
    total_fare: float = 0.0
    await_location_input: bool = False
    await_fare_input: bool = False

    @classmethod
    def from_firestore_doc(cls, doc: DocumentSnapshot) -> Optional[Self]:
        """Creates a User object from a Firestore document snapshot."""
        if doc.exists:
            return cls.model_validate(doc.to_dict())
        return None

    @classmethod
    def get_or_create_from_message_user(cls, from_user: telebot.types.User) -> Self:
        """Gets or creates a User object from a Telegram message user."""
        user_id = from_user.id
        user_id_str = str(user_id)
        user_ref = db.collection(USER_COLLECTION_NAME).document(user_id_str)
        user_doc = user_ref.get()

        user = cls.from_firestore_doc(user_doc)
        if user:
            return user

        new_user_data = {
            "user_id": from_user.id,
            "first_name": from_user.first_name,
            "last_name": from_user.last_name,
            "username": from_user.username,
        }
        user_ref.set(new_user_data)
        return cls.model_validate(new_user_data)

    def update_in_firestore(self):
        """Updates the corresponding Firestore document with the current User data."""
        user_ref = db.collection(USER_COLLECTION_NAME).document(str(self.user_id))
        user_ref.update(self.model_dump(exclude_unset=True))

    def get_all_shifts(self) -> List[Trip]:
        """Retrieves all trips associated with this user from Firestore."""
        trips_ref = (
            db.collection(SHIFT_COLLECTION_NAME)
            .where(filter=FieldFilter("user_id", "==", str(self.user_id)))
            .stream()
        )
        trips = [Trip.from_firestore_doc(trip_doc) for trip_doc in trips_ref]
        return [_ for _ in trips if _ is not None]

    def get_all_trips(self) -> List[Trip]:
        """Retrieves all trips associated with this user from Firestore."""
        trips_ref = (
            db.collection(TRIP_COLLECTION_NAME)
            .where(filter=FieldFilter("user_id", "==", str(self.user_id)))
            .stream()
        )
        trips = [Trip.from_firestore_doc(trip_doc) for trip_doc in trips_ref]
        return [_ for _ in trips if _ is not None]


def get_osm_location(lat: float, lon: float) -> Optional[str]:
    """Get the address from latitude and longitude using OSM Nominatim."""
    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        response = httpx.get(
            url,
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={
                "User-Agent": "ArBaakTaxi/0.0",
                "Referrer": "https://arbaak.com",
                "Accept-Language": "zh",
            },
        )
        response.raise_for_status()
        data = response.json()
        return " ".join(
            [
                data.get("road", ""),
                data.get("house_number", ""),
                data.get("village", ""),
                data.get("building", ""),
            ]
        ).strip()

    except httpx.HTTPStatusError as err:
        logging.error(f"Error fetching location from OSM: {err}")
        return None


def get_hk_geodata_location(lat: float, lon: float) -> Optional[str]:
    """Gets the address from latitude and longitude using the HK GeoData API."""
    easting, northing = transformer.transform(lat, lon)
    logging.warning(f"Easting {easting}, Northing {northing}")
    url = "https://geodata.gov.hk/gs/api/v1.0.0/identify"
    try:
        response = httpx.get(
            url, params={"x": northing, "y": easting, "lang": "zh"}, timeout=10
        )
        response.raise_for_status()
        data = response.json()
        address = data["results"][0]
        match address["type"]:
            case "LOT":
                location_name = [
                    address["addressInfo"][0].get("LOTNAME", ""),
                    address["addressInfo"][0].get("LOT_FULLNAME", ""),
                ]
                location_name = [name for name in location_name if name]
                return " ".join(location_name).replace("<br>", "").strip()
            case "ADDRESS":
                location_name = [
                    address["addressInfo"][0].get("caddress", ""),
                    address["addressInfo"][0].get("cname", ""),
                ]
                location_name = [name for name in location_name if name]
                return " ".join(location_name).replace("<br>", "").strip()
        logging.error(
            f"Unrecognised address type {address['type']} for address {address}"
        )
        return None

    except httpx.HTTPStatusError as err:
        logging.error(f"Error fetching location from HK GeoData: {err}")
        return None
    except (json.decoder.JSONDecodeError, KeyError, IndexError) as err:
        logging.error(f"Error parsing location response: {err}")
        return None


def create_keyboard(user: User) -> telebot.types.ReplyKeyboardMarkup:
    """Creates the keyboard with the appropriate button states based on the user's active trip/shift."""

    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    button_start_trip = telebot.types.KeyboardButton(text="上客")
    button_end_trip = telebot.types.KeyboardButton(text="落客")

    if user.active_trip:
        button_start_trip.request_location = False
        button_end_trip.request_location = True
    else:
        button_start_trip.request_location = True
        button_end_trip.request_location = False

    keyboard.row(button_start_trip, button_end_trip)
    return keyboard


def start(user: User, message: telebot.types.Message) -> None:
    bot.send_message(
        message.chat.id,
        f"喂，{user.first_name} 師傅！搵食工具準備好未？\n開工 /start_shift\n收工 "
        "/end_shift\n睇返之前啲job /get_trips",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )


def start_shift(user: User, message: telebot.types.Message) -> None:

    # Check if the user already has an active shift
    if user.active_shift:
        bot.send_message(
            message.chat.id,
            "你已經開咗工喇喎！想收工就用 /end_shift 啦。",
        )
        return

    # Create a new shift and save it to Firestore
    shift = Shift(
        user_id=str(user.user_id),
        start_time=datetime.now(timezone.utc),
    )
    shift.save_to_firestore()

    # Update the user's active_shift to the new shift ID
    user.active_shift = shift.shift_id
    user.update_in_firestore()

    bot.send_message(
        message.chat.id,
        "開工大吉！祝你日日爆job，晚晚call台唔停！",
        reply_markup=create_keyboard(user),
    )


def end_shift(user: User, message: telebot.types.Message):

    if user.active_trip:
        bot.send_message(
            message.chat.id,
            "仲有單 job 未搞掂喎！撳下面個『落客』制先。做完先收工啦。",
        )
        return

    if user.active_shift is None:
        bot.send_message(message.chat.id, "You don't have an active shift.")
        return

    shift = Shift.get_shift_by_id(user.active_shift)
    if shift is None:
        bot.send_message(
            message.chat.id,
            "Error: Active shift not found in database. Resetting shift status.",
        )
        user.active_trip = None
        user.update_in_firestore()
        return

    shift.end_time = datetime.now(timezone.utc)

    # Get all trips within the shift and calculate total trips and fare
    trips = shift.get_all_trips()
    shift.total_trips = len(trips)
    shift.total_fare = sum(trip.fare for trip in trips if trip.fare is not None)
    shift.update_in_firestore()

    # Unassign active_trip in the 'taxi-users' document
    user.active_shift = None
    user.update_in_firestore()

    shift_summary = f"""
收工啦，辛苦晒！\n今日總共做咗 {shift.total_trips} 單生意，\n埋單總數 {shift.total_fare:.2f} 蚊。\n唞夠聽日再嚟過啦！
    """
    bot.send_message(message.chat.id, shift_summary)

    bot.send_message(
        message.chat.id,
        "開工 /start_shift\n收工 /end_shift\n睇返之前啲job /get_trips",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )


def handle_location(user: User, message: telebot.types.Message) -> None:
    latitude = message.location.latitude
    longitude = message.location.longitude
    logging.warning(f"Latitude {latitude} Longitude {longitude}")

    if user.active_shift is None:
        bot.send_message(
            message.chat.id, "你未開工喎，師傅！用 /start_shift 開工先啦。"
        )
        return

    shift = Shift.get_shift_by_id(user.active_shift)

    location = get_osm_location(latitude, longitude)

    if not location:
        logging.warning(
            f"Latitude {latitude} Longitude {longitude} not found in Hong Kong."
        )
        bot.send_message(
            message.chat.id,
            "大佬，你而家喺邊度呀？好似唔係香港喎！一係你話我知你喺邊",
        )
        user.await_location_input = True
        user.update_in_firestore()
        return

    if user.active_trip:
        handle_end_trip(
            message,
            user=user,
            shift=shift,
            latitude=latitude,
            longitude=longitude,
            location=location,
        )
    else:
        handle_start_trip(
            message,
            user=user,
            shift=shift,
            latitude=latitude,
            longitude=longitude,
            location=location,
        )


def handle_custom_location(
    user: User,
    message: telebot.types.Message,
) -> None:

    if user.active_shift is None:
        bot.send_message(
            message.chat.id, "你未開工喎，師傅！用 /start_shift 開工先啦。"
        )
        return

    shift = Shift.get_shift_by_id(user.active_shift)

    location = message.text

    if not location:
        bot.send_message(
            message.chat.id,
            "大佬，你而家喺邊度呀？好似唔係香港喎！一係你話我知你喺邊",
        )
        user.await_location_input = True
        user.update_in_firestore()
        return

    if user.active_trip:
        handle_end_trip(message, user=user, shift=shift, location=location)
    else:
        handle_start_trip(message, user=user, shift=shift, location=location)


def handle_start_trip(
    message,
    user: User,
    shift: Shift,
    location: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> None:
    """Handles the logic for starting a new trip."""
    trip = Trip(
        user_id=str(user.user_id),
        shift_id=shift.shift_id,
        start_latitude=latitude,
        start_longitude=longitude,
        start_address=location,
        start_time=datetime.now(timezone.utc),
    )
    trip.save_to_firestore()

    # Update active_trip in the user object and Firestore
    user.active_trip = trip.trip_id
    user.await_location_input = False
    user.update_in_firestore()

    bot.send_message(
        message.chat.id,
        f"好，{location} 出發！記得撳『落客』入數啊！",
        reply_markup=create_keyboard(user),
    )


def handle_end_trip(
    message,
    user: User,
    shift: Shift,  # pylint: disable=unused-argument
    location: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> None:
    """Handles the logic for ending a trip."""

    if user.active_trip is None:
        bot.send_message(
            message.chat.id,
            "用 /start_shift 開工先啦",
        )
        return

    trip = Trip.get_trip_by_id(user.active_trip)
    if trip is None:
        bot.send_message(
            message.chat.id,
            "Error: Active trip not found in database. Resetting trip status.",
        )
        user.active_trip = None
        user.update_in_firestore()
        return

    # Update the end-related fields in the trip object
    trip.end_latitude = latitude
    trip.end_longitude = longitude
    trip.end_address = location
    trip.end_time = datetime.now(timezone.utc)
    trip.update_in_firestore()

    # Increment total_trips and update total_fare in the user object and Firestore
    user.await_location_input = False
    user.await_fare_input = True
    user.update_in_firestore()

    bot.send_message(
        message.chat.id,
        "OK！入埋車費！",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )


def process_fare_input(
    message: telebot.types.Message, user: User, shift: Shift, trip: Trip
) -> None:
    """Processes the fare input from the user and updates Firestore."""

    try:
        fare = float(message.text)
        if fare <= 0:
            raise ValueError("Fare must be a positive number.")

        trip.fare = fare
        trip.update_in_firestore()

        shift.total_trips += 1
        shift.total_fare += fare
        shift.update_in_firestore()

        user.total_trips += 1
        user.total_fare += fare
        user.active_trip = None
        user.await_fare_input = False
        user.update_in_firestore()

        bot.send_message(
            message.chat.id,
            f"收到，{fare:.2f} 蚊！而家做左{shift.total_trips:,g}單 ${shift.total_fare:,.2f} 生意， 繼續努力！",
        )

        bot.send_message(
            message.chat.id,
            "可以再撳『上客』接下一單，或者用 /end_shift 收工。",
            reply_markup=create_keyboard(user),
        )

    except ValueError:
        bot.send_message(
            message.chat.id, "吓？呢個價錢有啲古怪喎... 再入多次啦，唔該晒！"
        )


def get_trips(user: User, message: telebot.types.Message) -> None:

    hk_tz = pytz.timezone("Asia/Hong_Kong")
    trips = user.get_all_trips()

    if not trips:
        bot.send_message(message.chat.id, "You have no past trips.")
        return

    trips.sort(key=lambda trip: trip.start_time, reverse=True)
    # Generate CSV data
    csv_data = StringIO()
    writer = csv.writer(csv_data)
    writer.writerow(
        [
            "Shift ID",
            "Trip ID",
            "Start Time",
            "Start Address",
            "End Time",
            "End Address",
            "Fare",
        ]
    )
    for trip in trips:
        trip_data = trip.model_dump()
        start_time_str = (
            trip_data["start_time"].astimezone(hk_tz).strftime("%Y-%m-%d %H:%M:%S")
            if trip_data["start_time"]
            else "N/A"
        )
        end_time_str = (
            trip_data["end_time"].astimezone(hk_tz).strftime("%Y-%m-%d %H:%M:%S")
            if trip_data["end_time"]
            else "N/A"
        )
        fare_str = f'${trip_data["fare"]:.2f}' if trip_data["fare"] else "N/A"
        writer.writerow(
            [
                trip.shift_id,
                trip.trip_id,
                start_time_str,
                trip.start_address,
                end_time_str,
                trip.end_address,
                fare_str,
            ]
        )

    # Send the CSV file
    csv_data.seek(0)
    file = telebot.types.InputFile(csv_data, file_name="trips.csv")
    bot.send_document(message.chat.id, file)


@app.route("/handle_telegram_update", methods=["POST"])
def handle_telegram_update(request):
    """Handles incoming Telegram updates using webhooks."""
    if request.method == "POST":
        update = telebot.types.Update.de_json(request.get_json())
        user = User.get_or_create_from_message_user(update.message.from_user)

        if update.message.content_type == "location":
            logging.info(
                f"Location received from user {user.user_id} {user.first_name}"
            )
            handle_location(user=user, message=update.message)
        elif update.message.content_type == "text":
            match update.message.text:
                case "/start":
                    logging.info(
                        f"`/start` received from user {user.user_id} {user.first_name}"
                    )
                    start(user=user, message=update.message)
                case "/start_shift":
                    logging.info(
                        f"`/start_shift` received from user {user.user_id} {user.first_name}"
                    )
                    start_shift(user=user, message=update.message)
                case "/end_shift":
                    logging.info(
                        f"`/end_shift` received from user {user.user_id} {user.first_name}"
                    )
                    end_shift(user=user, message=update.message)
                case "/get_trips":
                    logging.info(
                        f"`/get_trips` received from user {user.user_id} {user.first_name}"
                    )
                    get_trips(user=user, message=update.message)
                case _:
                    logging.info(
                        f"Text received from user {user.user_id} {user.first_name}"
                    )
                    logging.info(f"Await location input: {user.await_location_input}")
                    logging.info(f"Await fare input: {user.await_fare_input}")
                    if user.await_location_input:
                        handle_custom_location(user=user, message=update.message)
                    elif user.await_fare_input:
                        active_shift = Shift.get_shift_by_id(user.active_shift)
                        active_trip = Trip.get_trip_by_id(user.active_trip)
                        if not (
                            (active_shift is None)
                            or (active_trip is None)
                            or (active_trip.end_time is None)
                        ):
                            process_fare_input(
                                message=update.message,
                                user=user,
                                shift=active_shift,
                                trip=active_trip,
                            )
        return jsonify({"status": "OK"}), 200
    return jsonify({"error": "Method not allowed"}), 405


if __name__ == "__main__":

    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
