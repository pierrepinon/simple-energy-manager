# Simple Energy Manager

Home Assistant HACS integration that schedules devices on the cheapest electricity price slots, respects meter power limits, and exposes a planning calendar.

## Features

- Reads electricity price forecasts from any sensor exposing a `prices` attribute
- Computes the cheapest time slots to run each configured device
- Respects your subscribed meter power limit (VA) to avoid overload
- Exposes a **calendar** entity with all planned sessions and their estimated cost
- Exposes a **binary sensor** per device, indicating whether it should be running right now
- `plan_device` service to schedule any device on demand and get the start time and cost in return

## Requirements

- Home Assistant 2025.4 or later
- A sensor providing electricity price forecasts (e.g. [Forecast Solar](https://www.home-assistant.io/integrations/forecast_solar/), [Amber Electric](https://www.home-assistant.io/integrations/amberelectric/), or any custom sensor)
- The price sensor must expose a `prices` attribute — a list of objects with `timestamp` and `price` fields
- A sensor exposing your subscribed meter power limit with `device_class: apparent_power`

## Installation

### HACS

1. Add this repository as a custom repository in HACS
2. Install **Simple Energy Manager**
3. Restart Home Assistant

### Manual

Copy the `custom_components/energy_planner` folder into your `config/custom_components/` directory and restart Home Assistant.

## Configuration

1. Go to **Settings → Integrations → Add integration** and search for **Energy Planner**
2. Select your price forecast sensor and your meter power limit sensor
3. Click the **+** button on the integration card to add a device (water heater or any `water_heater` entity)
4. For each device, configure its power consumption (W) and the minimum daily usage duration (minutes)

## Service: `plan_device`

Schedule any device within a time window and get the optimal start time back.

```yaml
service: energy_planner.plan_device
data:
  duration: 90          # minutes
  power: 1500           # watts
  earliest_start: "2025-01-15T08:00:00"
  latest_end: "2025-01-15T22:00:00"
  label: "Washing machine"
response_variable: result
# result.start  → ISO 8601 timestamp of optimal start
# result.cost   → estimated cost in €
```

The service checks existing scheduled loads and won't exceed the meter power limit. The event is added to the planning calendar automatically.

## Calendar entity

The calendar **Planning Energy Planner** shows all upcoming sessions with their estimated cost in the description field. It can be displayed on any Home Assistant dashboard using the calendar card.

## Binary sensors

One binary sensor is created per configured device. It is `on` when the device should be running according to the current schedule, and exposes the upcoming `scheduled_slots` as an attribute (list of `{start, end, cost}` objects).
