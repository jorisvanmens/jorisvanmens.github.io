import requests
import datetime

def get_tomorrows_pricing():
    """
    Fetches tomorrow's hourly pricing data from the GridX API for PGE,
    ratename EV2AS, representativeCircuitId 042211101, and CCA MCE.
    """
    # Get tomorrow's date in the required YYYYMMDD format
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_str = tomorrow.strftime('%Y%m%d')

    # API Endpoint URL for production
    url = "https://pge-pe-api.gridx.com/v1/getPricing"

    # Parameters for the API request
    params = {
        'utility': 'PGE',
        'market': 'DAM',
        'startdate': tomorrow_str,
        'enddate': tomorrow_str,
        'ratename': 'EV2A',
        'representativeCircuitId': '042211101',
        'program': 'CalFUSE',
        'cca': 'MCE'
    }

    try:
        # Make the GET request to the API
        response = requests.get(url, params=params)
        # Raise an exception for bad status codes (4xx or 5xx)
        response.raise_for_status()
        # Return the JSON response
        return response.json()
    except requests.exceptions.RequestException as e:
        return f"An error occurred: {e}"

def format_and_print_pricing(pricing_data):
    """
    Formats and prints the pricing data in a human-readable table.
    """
    # Check if the API returned an error string
    if isinstance(pricing_data, str):
        print(pricing_data)
        return

    # Safely access the nested pricing data
    try:
        price_details = pricing_data['data'][0]['priceDetails']
        header_info = pricing_data['data'][0]['priceHeader']
        start_date = datetime.datetime.fromisoformat(header_info['startTime']).strftime('%Y-%m-%d')
    except (KeyError, IndexError, TypeError):
        print("Could not parse pricing data. The structure may have changed.")
        print("Received data:", pricing_data)
        return

    # Print the table header
    print(f"\n--- Hourly Electricity Pricing for {start_date} ---")
    print("-" * 52)
    print(f"| {'Start Time':<25} | {'Price ($c/kWh)':>20} |")
    print("-" * 52)

    # Print each row of pricing data
    for detail in price_details:
        timestamp = datetime.datetime.fromisoformat(detail['startIntervalTimeStamp'])
        formatted_time = timestamp.strftime('%Y-%m-%d %H:%M:%S %Z')
        price = float(detail['intervalPrice']) * 100
        print(f"| {formatted_time:<25} | {price:20.6f} |")

    # Print the table footer
    print("-" * 52)


if __name__ == "__main__":
    pricing_data = get_tomorrows_pricing()
    format_and_print_pricing(pricing_data)