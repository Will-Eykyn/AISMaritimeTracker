import asyncio
import websockets
import json
import time
import re
from datetime import datetime, timezone
import folium
from folium import Element
import base64

from config import LAT_MIN, LAT_MAX, LON_MIN, LON_MAX

from dotenv import load_dotenv
import os

import signal
signal.signal(signal.SIGTERM, lambda *_: exit(0))

MAX_TRACK_POINTS    = 200
MAP_UPDATE_INTERVAL = 5   # seconds between map regenerations

CLASS_COLORS = {
    "Class A":    "#e74c3c",
    "Class B":    "#3498db",
    "Long Range": "#ff69b4",
    "Unknown":    "#888888",
}

# ITU-R M.1371 ship type codes → human-readable labels
SHIP_TYPES = {
    0: "Not available",
    20: "Wing in ground", 21: "Wing in ground (hazcat A)", 22: "Wing in ground (hazcat B)",
    23: "Wing in ground (hazcat C)", 24: "Wing in ground (hazcat D)",
    30: "Fishing",
    31: "Towing", 32: "Towing (large)",
    33: "Dredging/underwater ops", 34: "Diving ops", 35: "Military",
    36: "Sailing", 37: "Pleasure craft",
    40: "High speed craft", 41: "HSC (hazcat A)", 42: "HSC (hazcat B)",
    43: "HSC (hazcat C)", 44: "HSC (hazcat D)", 49: "HSC (no info)",
    50: "Pilot vessel", 51: "Search and rescue", 52: "Tug", 53: "Port tender",
    54: "Anti-pollution", 55: "Law enforcement", 58: "Medical transport",
    59: "Non-combatant ship",
    60: "Passenger", 61: "Passenger (hazcat A)", 62: "Passenger (hazcat B)",
    63: "Passenger (hazcat C)", 64: "Passenger (hazcat D)", 69: "Passenger (no info)",
    70: "Cargo", 71: "Cargo (hazcat A)", 72: "Cargo (hazcat B)",
    73: "Cargo (hazcat C)", 74: "Cargo (hazcat D)", 79: "Cargo (no info)",
    80: "Tanker", 81: "Tanker (hazcat A)", 82: "Tanker (hazcat B)",
    83: "Tanker (hazcat C)", 84: "Tanker (hazcat D)", 89: "Tanker (no info)",
    90: "Other", 99: "Other (no info)",
}

def format_eta(eta):
    """Convert AISStream ETA dict or string to a readable format."""
    if not eta:
        return None
    if isinstance(eta, dict):
        month  = eta.get('Month',  0)
        day    = eta.get('Day',    0)
        hour   = eta.get('Hour',   24)   # 24 = not available in AIS spec
        minute = eta.get('Minute', 60)   # 60 = not available
        if month == 0 and day == 0:
            return None
        months = ['','Jan','Feb','Mar','Apr','May','Jun',
                  'Jul','Aug','Sep','Oct','Nov','Dec']
        month_str = months[month] if 1 <= month <= 12 else str(month)
        time_str  = f" {hour:02d}:{minute:02d}" if hour != 24 else ""
        return f"{month_str} {day}{time_str}"
    if isinstance(eta, str):
        cleaned = eta.strip()
        return cleaned if cleaned else None
    return None


def clean_text(value):
    """Strip AIS padding (@) and non-printable characters from text fields."""
    if not value:
        return None
    cleaned = re.sub(r'[^\x20-\x7E]', '', str(value))  # keep printable ASCII only
    cleaned = cleaned.strip('@').strip()
    return cleaned if cleaned else None


def decode_ship_type(code):
    if code is None:
        return None
    code = int(code)
    if code in SHIP_TYPES:
        return SHIP_TYPES[code]
    # Fall back to range buckets
    if 20 <= code <= 29: return "Wing in ground"
    if 40 <= code <= 49: return "High speed craft"
    if 60 <= code <= 69: return "Passenger"
    if 70 <= code <= 79: return "Cargo"
    if 80 <= code <= 89: return "Tanker"
    if 90 <= code <= 99: return "Other"
    return f"Type {code}"


load_dotenv()
token = os.getenv("AISSTREAM_TOKEN")
if not token:
    raise ValueError("AISSTREAM_TOKEN not found in environment or .env file")


def make_colored_svg_url(svg_text, color):
    style  = f'<style>path,polygon,rect,circle,ellipse{{fill:{color} !important;}}</style>'
    colored = re.sub(r'(<svg[^>]*>)', r'\1' + style, svg_text, count=1)
    return f"data:image/svg+xml;base64,{base64.b64encode(colored.encode('utf-8')).decode('utf-8')}"


async def connect_ais_stream():
    ships         = {}
    last_map_update = 0
    reconnect_delay = 5

    while True:
        try:
            print("Connecting to AIS stream...")
            async with websockets.connect("wss://stream.aisstream.io/v0/stream") as websocket:
                subscribe_message = {
                    "APIKey": token,
                    "BoundingBoxes": [[[LAT_MIN, LON_MIN], [LAT_MAX, LON_MAX]]],
                    "FilterMessageTypes": [
                        "PositionReport",
                        "ExtendedClassBPositionReport",
                        "StandardClassBPositionReport",
                        "LongRangeAisBroadcastMessage",
                        "ShipStaticData",          # vessel name, dimensions, type, etc.
                    ],
                }
                await websocket.send(json.dumps(subscribe_message))
                reconnect_delay = 5
                print("Connected.")

                async for message_json in websocket:
                    print(f"Raw message: {message_json}\n\n")
                    message      = json.loads(message_json)
                    message_type = message["MessageType"]

                    # --- Position messages ---
                    if message_type in ["PositionReport",
                                        "ExtendedClassBPositionReport",
                                        "StandardClassBPositionReport",
                                        "LongRangeAisBroadcastMessage"]:

                        ais_message = message['Message'][message_type]
                        mmsi        = ais_message['UserID']
                        print(f"[{datetime.now(timezone.utc)}] ShipId: {mmsi}"
                              f" Lat: {ais_message['Latitude']} Lon: {ais_message['Longitude']}")

                        ais_class = {
                            "PositionReport":               "Class A",
                            "StandardClassBPositionReport": "Class B",
                            "ExtendedClassBPositionReport": "Class B",
                            "LongRangeAisBroadcastMessage": "Long Range",
                        }.get(message_type, "Unknown")

                        new_point = {
                            "lat":       ais_message['Latitude'],
                            "lon":       ais_message['Longitude'],
                            "timestamp": datetime.now(timezone.utc),
                            "heading":   ais_message.get("TrueHeading"),
                            "cog":       ais_message.get("Cog"),
                            "sog":       ais_message.get("Sog"),
                        }

                        if mmsi not in ships:
                            ships[mmsi] = {
                                "name":      message['MetaData']['ShipName'],
                                "ais_class": ais_class,
                                "track":     [],
                                "static":    {},
                            }

                        ships[mmsi]["track"].append(new_point)
                        if len(ships[mmsi]["track"]) > MAX_TRACK_POINTS:
                            ships[mmsi]["track"].pop(0)

                    # --- Static / voyage data (sent every ~6 min by Class A, less often by B) ---
                    elif message_type == "ShipStaticData":
                        ais_message = message['Message']['ShipStaticData']
                        mmsi        = ais_message['UserID']

                        # Dimensions: AIS reports distance bow/stern/port/starboard from
                        # the transponder antenna. Sum gives LOA and beam.
                        bow   = ais_message.get('DimensionToBow',        0) or 0
                        stern = ais_message.get('DimensionToStern',      0) or 0
                        port  = ais_message.get('DimensionToPort',       0) or 0
                        stbd  = ais_message.get('DimensionToStarboard',  0) or 0
                        loa   = bow + stern
                        beam  = port + stbd

                        ship_type_code = ais_message.get('ShipType') or ais_message.get('Type')

                        static = {
                            "imo":         ais_message.get('ImoNumber'),
                            "callsign":    clean_text(ais_message.get('CallSign')),
                            "ship_type":   decode_ship_type(ship_type_code),
                            "loa":         loa  if loa  > 0 else None,
                            "beam":        beam if beam > 0 else None,
                            "draft":       ais_message.get('Draught'),
                            "destination": clean_text(ais_message.get('Destination')),
                            "eta":         format_eta(ais_message.get('Eta')),
                        }

                        if mmsi in ships:
                            ships[mmsi]["static"] = static
                            # Update name if we now have a better one
                            name = (ais_message.get('Name') or '').strip()
                            if name:
                                ships[mmsi]["name"] = name
                        else:
                            # Static data arrived before any position report
                            ships[mmsi] = {
                                "name":      (ais_message.get('Name') or str(mmsi)).strip(),
                                "ais_class": "Unknown",
                                "track":     [],
                                "static":    static,
                            }

                    now = time.time()
                    if now - last_map_update >= MAP_UPDATE_INTERVAL:
                        generate_map(ships)
                        last_map_update = now

        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nStopped by user. Closing.")
            break
        except Exception as e:
            print(f"\nConnection lost: {e}")
            print(f"Reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


def generate_map(ships, map_file="map.html"):
    center_lat = (LAT_MIN + LAT_MAX) / 2
    center_lon = (LON_MIN + LON_MAX) / 2
    m = folium.Map(location=[center_lat, center_lon], zoom_start=9, tiles='CartoDB positron')

    folium.Rectangle(
        bounds=[[LAT_MIN, LON_MIN], [LAT_MAX, LON_MAX]],
        color="blue", fill=False
    ).add_to(m)
    m.fit_bounds([[LAT_MIN, LON_MIN], [LAT_MAX, LON_MAX]], padding=(30, 30))

    svg_path = "boatMarker.svg"
    with open(svg_path, "r", encoding="utf-8") as f:
        svg_text = f.read()
    svg_urls = {cls: make_colored_svg_url(svg_text, color) for cls, color in CLASS_COLORS.items()}

    # ── Build vessel data dict for the side panel ──────────────────────────────
    vessel_data = {}
    for ship_id, info in ships.items():
        track = info["track"]
        if not track:
            continue
        latest  = track[-1]
        heading = latest.get("heading")
        cog     = latest.get("cog")
        sog     = latest.get("sog")
        s       = info.get("static", {})

        vessel_data[str(ship_id)] = {
            "name":        info.get("name", str(ship_id)),
            "ais_class":   info.get("ais_class", "Unknown"),
            "mmsi":        ship_id,
            "imo":         s.get("imo")         or "—",
            "callsign":    s.get("callsign")    or "—",
            "ship_type":   s.get("ship_type")   or "—",
            "loa":         f"{s['loa']} m"      if s.get("loa")   else "—",
            "beam":        f"{s['beam']} m"     if s.get("beam")  else "—",
            "draft":       f"{s['draft']} m"    if s.get("draft") else "—",
            "destination": s.get("destination") or "—",
            "eta":         s.get("eta")         or "—",
            "speed":       f"{sog} kts"         if sog     is not None                     else "—",
            "heading":     f"{heading}°"        if heading is not None and heading != 511  else "—",
            "cog":         f"{cog}°"            if cog     is not None                     else "—",
            "last_seen":   latest["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"),
        }

    vessel_data_json = json.dumps(vessel_data)

    # ── Side panel HTML + CSS + JS ─────────────────────────────────────────────
    map_name = m.get_name()
    panel_html = f"""
<style>
  #vessel-panel {{
    position: fixed; top: 0; right: -420px; width: 400px; height: 100vh;
    background: #fff; z-index: 9999;
    box-shadow: -4px 0 24px rgba(0,0,0,0.18);
    transition: right 0.32s cubic-bezier(0.4,0,0.2,1);
    overflow-y: auto;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    font-size: 14px; color: #222;
  }}
  #vessel-panel.open {{ right: 0; }}
  #panel-header {{
    display: flex; justify-content: space-between; align-items: flex-start;
    padding: 20px 20px 12px; border-bottom: 1px solid #eee;
    position: sticky; top: 0; background: #fff; z-index: 1;
  }}
  #panel-title {{ margin: 0; font-size: 17px; font-weight: 700; line-height: 1.3; }}
  #panel-subtitle {{ margin: 4px 0 0; font-size: 12px; color: #888; }}
  #panel-close {{
    background: none; border: none; font-size: 22px; cursor: pointer;
    color: #aaa; padding: 0 0 0 12px; line-height: 1; flex-shrink: 0;
  }}
  #panel-close:hover {{ color: #333; }}
  #panel-body {{ padding: 16px 20px 24px; }}
  .panel-section {{ margin-bottom: 20px; }}
  .panel-section-title {{
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: #999; margin: 0 0 8px;
  }}
  .panel-row {{
    display: flex; justify-content: space-between;
    padding: 5px 0; border-bottom: 1px solid #f4f4f4;
  }}
  .panel-row:last-child {{ border-bottom: none; }}
  .panel-label {{ color: #888; }}
  .panel-value {{ font-weight: 500; text-align: right; max-width: 60%; }}
  .panel-badge {{
    display: inline-block; padding: 2px 8px; border-radius: 20px;
    font-size: 12px; font-weight: 600; color: #fff; margin-bottom: 12px;
  }}
  .panel-link {{
    display: block; margin-top: 16px; padding: 10px 16px; border-radius: 6px;
    background: #f5f5f5; color: #333; text-decoration: none; font-weight: 500;
    text-align: center; transition: background 0.15s;
  }}
  .panel-link:hover {{ background: #e8e8e8; }}
</style>

<div id="vessel-panel">
  <div id="panel-header">
    <div>
      <h2 id="panel-title">—</h2>
      <p id="panel-subtitle">—</p>
    </div>
    <button id="panel-close" onclick="closeVesselPanel()">&times;</button>
  </div>
  <div id="panel-body"></div>
</div>

<script>
var _vesselData = {vessel_data_json};

function openVesselPanel(mmsi) {{
  var d = _vesselData[String(mmsi)];
  if (!d) return;

  var badgeColor = {{ 'Class A': '#e74c3c', 'Class B': '#3498db', 'Long Range': '#ff69b4' }}[d.ais_class] || '#888';

  document.getElementById('panel-title').textContent    = d.name;
  document.getElementById('panel-subtitle').textContent = 'MMSI: ' + d.mmsi;
  document.getElementById('panel-body').innerHTML = `
    <span class="panel-badge" style="background:${{badgeColor}}">${{d.ais_class}}</span>

    <div class="panel-section">
      <p class="panel-section-title">Live Data</p>
      <div class="panel-row"><span class="panel-label">Speed</span><span class="panel-value">${{d.speed}}</span></div>
      <div class="panel-row"><span class="panel-label">Heading</span><span class="panel-value">${{d.heading}}</span></div>
      <div class="panel-row"><span class="panel-label">Course over ground</span><span class="panel-value">${{d.cog}}</span></div>
      <div class="panel-row"><span class="panel-label">Last seen</span><span class="panel-value">${{d.last_seen}}</span></div>
    </div>

    <div class="panel-section">
      <p class="panel-section-title">Vessel Info</p>
      <div class="panel-row"><span class="panel-label">Type</span><span class="panel-value">${{d.ship_type}}</span></div>
      <div class="panel-row"><span class="panel-label">IMO</span><span class="panel-value">${{d.imo}}</span></div>
      <div class="panel-row"><span class="panel-label">Call sign</span><span class="panel-value">${{d.callsign}}</span></div>
    </div>

    <div class="panel-section">
      <p class="panel-section-title">Dimensions</p>
      <div class="panel-row"><span class="panel-label">Length overall</span><span class="panel-value">${{d.loa}}</span></div>
      <div class="panel-row"><span class="panel-label">Beam</span><span class="panel-value">${{d.beam}}</span></div>
      <div class="panel-row"><span class="panel-label">Draft</span><span class="panel-value">${{d.draft}}</span></div>
    </div>

    <div class="panel-section">
      <p class="panel-section-title">Voyage</p>
      <div class="panel-row"><span class="panel-label">Destination</span><span class="panel-value">${{d.destination}}</span></div>
      <div class="panel-row"><span class="panel-label">ETA</span><span class="panel-value">${{d.eta}}</span></div>
    </div>

  `;

  document.getElementById('vessel-panel').classList.add('open');
}}

function closeVesselPanel() {{
  document.getElementById('vessel-panel').classList.remove('open');
}}
</script>
"""
    m.get_root().html.add_child(Element(panel_html))

    # ── Trail registry + map click hides trails and closes panel ──────────────
    setup_js = f"""
<script>
document.addEventListener('DOMContentLoaded', function() {{
    window._allTrails = [];
    window['{map_name}'].on('click', function() {{
        window._allTrails.forEach(function(t) {{
            if (t.poly._trailVisible) {{
                t.poly.setStyle({{opacity: 0}});
                t.dot.setStyle({{opacity: 0, fillOpacity: 0}});
                t.poly._trailVisible = false;
            }}
        }});
        closeVesselPanel();
    }});
}});
</script>"""
    m.get_root().html.add_child(Element(setup_js))

    # ── Vessel markers ─────────────────────────────────────────────────────────
    for ship_id, info in ships.items():
        track = info["track"]
        if not track:
            continue

        latest    = track[-1]
        ais_class = info.get("ais_class", "Unknown")
        color     = CLASS_COLORS.get(ais_class, CLASS_COLORS["Unknown"])
        svg_url   = svg_urls.get(ais_class, svg_urls["Unknown"])

        heading = latest.get("heading")
        cog     = latest.get("cog")
        sog     = latest.get("sog")

        if heading is not None and heading != 511:
            rotation = heading
        elif cog is not None:
            rotation = cog
        else:
            rotation = 0

        heading_str = f"{heading}°" if heading is not None and heading != 511 else "N/A"
        cog_str     = f"{cog}°"     if cog     is not None                     else "N/A"
        sog_str     = f"{sog} kts"  if sog     is not None                     else "N/A"

        popup_text = (
            f'<div style="min-width:220px; line-height:1.7;">'
            f"<b>{info['name']}</b> ({ship_id}) &middot; {ais_class}<br>"
            f"Heading: {heading_str}&nbsp;&nbsp; COG: {cog_str}&nbsp;&nbsp; Speed: {sog_str}<br>"
            f"{latest['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}<br>"
            f'<a href="#" onclick="openVesselPanel({ship_id}); return false;" '
            f'style="color:#3498db; font-weight:600;">Learn more →</a>'
            f"</div>"
        )

        # Trail layers (hidden by default)
        poly_var = None
        dot_var  = None

        if len(track) > 1:
            track_coords = [[p["lat"], p["lon"]] for p in track]
            polyline = folium.PolyLine(track_coords, color=color, weight=2, opacity=0)
            polyline.add_to(m)
            poly_var = polyline.get_name()

            first = track[0]
            start_dot = folium.CircleMarker(
                location=[first["lat"], first["lon"]],
                radius=5, color=color, fill=True, fill_color=color,
                opacity=0, fill_opacity=0,
                popup=folium.Popup(
                    f'<div style="min-width:180px;"><b>{info["name"]}</b><br>'
                    f'First seen: {first["timestamp"].strftime("%H:%M:%S UTC")}</div>',
                    max_width=250
                ),
            )
            start_dot.add_to(m)
            dot_var = start_dot.get_name()

        marker     = add_svg_marker(m, latest["lat"], latest["lon"], rotation, svg_url,
                                    popup=folium.Popup(popup_text, max_width=300))
        marker_var = marker.get_name()

        if poly_var and dot_var:
            toggle_js = f"""
<script>
document.addEventListener('DOMContentLoaded', function() {{
    var poly = {poly_var};
    var dot  = {dot_var};
    poly._trailVisible = false;
    window._allTrails.push({{poly: poly, dot: dot}});
    {marker_var}.on('click', function(e) {{
        L.DomEvent.stopPropagation(e);
        if (poly._trailVisible) {{
            poly.setStyle({{opacity: 0}});
            dot.setStyle({{opacity: 0, fillOpacity: 0}});
            poly._trailVisible = false;
        }} else {{
            poly.setStyle({{opacity: 0.7}});
            dot.setStyle({{opacity: 1, fillOpacity: 0.8}});
            poly._trailVisible = true;
        }}
    }});
}});
</script>"""
            m.get_root().html.add_child(Element(toggle_js))

    m.save(map_file)


def add_svg_marker(map_obj, lat, lon, cog, svg_url, popup=None, icon_size=(32, 32)):
    rotation  = cog
    icon_html = f'''
    <div style="transform: rotate({rotation}deg); transform-origin: center; width:{icon_size[0]}px; height:{icon_size[1]}px;">
        <img src="{svg_url}" width="{icon_size[0]}" height="{icon_size[1]}"/>
    </div>'''
    icon   = folium.DivIcon(html=icon_html, icon_size=icon_size,
                            icon_anchor=(icon_size[0] // 2, icon_size[1] // 2))
    marker = folium.Marker(location=[lat, lon], icon=icon, popup=popup)
    marker.add_to(map_obj)
    return marker


if __name__ == "__main__":
    try:
        asyncio.run(connect_ais_stream())
    except KeyboardInterrupt:
        print("\nProgram terminated by user")
