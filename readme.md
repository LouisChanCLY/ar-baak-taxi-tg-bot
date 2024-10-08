# Ar Baak Taxi Telegram Bot

This is a handy telegram bot designed to help Taxi drivers in Hong Kong in tracking
their trips and earnings. The telegram bot can be subscribed here: <https://t.me/ar_baak_taxi_bot>

To use the telegram bot, you may have to share your location with the bot during
pick up and drop off. The location detection service is provided by the Hong Kong
GeoData Store under the Common Spatial Data Infrastructure Portal.

## Reverse Geocoding

When you use the bot, we will use OSM Nominatim to reverse geocode your location
into a valid address in Hong Kong. This data & service is made available under the
Open Database License (ODbL) attributed to [OpenStreetMap](openstreetmap.org/copyright).

## Commands

- `/start`: Get started with the bot
- `/start_shift`: Start a new shift
- `/end_shift`: End your current shift
- `/get_trips`: Get an csv export of your recent trips
- `/get_all_trips`: Get an csv export of all your trips

<a href="https://www.buymeacoffee.com/louischan" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="41" width="174"></a>
