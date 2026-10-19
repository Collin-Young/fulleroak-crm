from __future__ import annotations

"""
Map Generator module for creating aerial images with property boundaries
Uses folium for interactive maps and Selenium for screenshot capture
"""
import io
import logging
import time
import os
import math
from typing import List, Tuple, Optional

try:
    import folium
    from folium.plugins import MarkerCluster
    FOLIUM_AVAILABLE = True
except ImportError:
    FOLIUM_AVAILABLE = False
    folium = None

try:
    from PIL import Image
    from PIL import ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    Image = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MapGenerator:
    """Generate aerial/satellite maps with property boundary overlays"""
    
    def __init__(self, zoom_start=18, tiles='Hybrid Satellite', fit_max_zoom=19):
        """
        Initialize map generator
        
        Args:
            zoom_start: Default zoom level (higher = more zoomed in)
            tiles: Map tile source label.
        """
        if not FOLIUM_AVAILABLE:
            raise ImportError("folium is required for map generation. Install with: pip install folium")
        
        self.zoom_start = zoom_start
        self.tiles = tiles
        self.fit_max_zoom = fit_max_zoom
        self.map = None
        
    def create_map(self, center_lat: float, center_lon: float, 
                   coordinates: List[List[Tuple[float, float]]],
                   zoom: Optional[int] = None) -> folium.Map:
        """
        Create a folium map with property boundary overlay
        
        Args:
            center_lat: Center latitude
            center_lon: Center longitude
            coordinates: List of coordinate rings [(lat, lon), ...]
            zoom: Zoom level (uses default if None)
            
        Returns:
            folium.Map object
        """
        zoom = zoom or self.zoom_start
        
        # Create a hybrid satellite map: imagery base with roads/place labels overlaid.
        m = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=zoom,
            zoom_control=False,
            max_zoom=22,
            tiles=None
        )

        folium.TileLayer(
            tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attr='Esri World Imagery',
            name='Satellite Imagery',
            overlay=False,
            control=False,
            max_zoom=22,
            max_native_zoom=19
        ).add_to(m)

        folium.TileLayer(
            tiles='https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}',
            attr='Esri World Transportation',
            name='Roads',
            overlay=True,
            control=False,
            max_zoom=22,
            max_native_zoom=19
        ).add_to(m)

        folium.TileLayer(
            tiles='https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
            attr='Esri World Boundaries and Places',
            name='Labels',
            overlay=True,
            control=False,
            max_zoom=22,
            max_native_zoom=19
        ).add_to(m)
        
        # Add property boundary
        all_polygon_coords = []
        for i, ring in enumerate(coordinates):
            if not ring:
                continue
                
            # Convert (lat, lon) to [lat, lon] for folium
            polygon_coords = [[lat, lon] for lat, lon in ring]
            
            # Close the polygon if not already closed
            if polygon_coords[0] != polygon_coords[-1]:
                polygon_coords.append(polygon_coords[0])

            all_polygon_coords.extend(polygon_coords)
            
            # Add polygon with high contrast styling for satellite imagery.
            folium.Polygon(
                locations=polygon_coords,
                color='#FFD400',
                fill=True,
                fill_color='#FFD400',
                fill_opacity=0.18,
                weight=5,
                popup='Property Boundary %d' % (i+1)
            ).add_to(m)

        if all_polygon_coords:
            lats = [lat for lat, _lon in all_polygon_coords]
            lons = [lon for _lat, lon in all_polygon_coords]
            m.fit_bounds(
                [[min(lats), min(lons)], [max(lats), max(lons)]],
                padding=(12, 12),
                max_zoom=self.fit_max_zoom
            )
        
        self.map = m
        return m
    
    def save_map(self, output_path: str, width=2550, height=1782):
        """
        Save map as image using Selenium to capture screenshot
        
        Args:
            output_path: Path to save the image
            width: Image width in pixels
            height: Image height in pixels
        """
        if self.map is None:
            raise ValueError("Map not created yet. Call create_map() first.")
        
        # Save as HTML first
        html_path = output_path.replace('.png', '.html').replace('.jpg', '.html')
        self.map.save(html_path)
        logger.info("Map saved as HTML: %s", html_path)
        
        # Convert HTML to image using Selenium
        image_data = self._html_to_image_selenium(html_path, width, height)
        
        if image_data:
            with open(output_path, 'wb') as f:
                f.write(image_data)
            logger.info("Map saved as image: %s", output_path)
            return output_path
        else:
            logger.warning("Could not convert to image, using static fallback")
            # Fall back to static image
            static_path = output_path.replace('.png', '_static.png')
            self.create_static_map_image(
                center_lat=self.map.location[0],
                center_lon=self.map.location[1],
                coordinates=self._get_coordinates_from_map(),
                output_path=static_path,
                width=width,
                height=height
            )
            return static_path
    
    def _get_coordinates_from_map(self):
        """Extract coordinates from the map object"""
        # This is a helper to get coordinates back from the map
        # In practice, we should pass coordinates explicitly
        return []
    
    def _html_to_image_selenium(self, html_path: str, width: int, height: int) -> Optional[bytes]:
        """
        Convert HTML to image using undetected Chrome driver
        Returns image bytes or None
        """
        try:
            import undetected_chromedriver as uc
            from selenium.webdriver.chrome.options import Options
            
            options = uc.ChromeOptions()
            options.add_argument('--headless=new')
            options.add_argument('--window-size=%d,%d' % (width, height))
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            
            driver = uc.Chrome(options=options)
            
            # Load the HTML file
            abs_path = os.path.abspath(html_path)
            driver.get("file://%s" % abs_path)
            
            # Wait for map to load (wait for tiles to load)
            time.sleep(5)
            
            # Get screenshot
            png = driver.get_screenshot_as_png()
            driver.quit()
            
            return png
            
        except ImportError:
            logger.warning("Undetected ChromeDriver not available for HTML to image conversion")
            return None
        except Exception as e:
            logger.warning("Selenium conversion failed: %s", e)
            import traceback
            logger.warning(traceback.format_exc())
            return None
    
    def create_static_map_image(self, center_lat: float, center_lon: float,
                                coordinates: List[List[Tuple[float, float]]],
                                output_path: str,
                                width: int = 800, height: int = 600) -> str:
        """
        Create a static map image using PIL
        Draws property boundary on a simple background
        """
        if not PIL_AVAILABLE:
            raise ImportError("PIL/Pillow is required. Install with: pip install Pillow")
        
        # Create a blank image with light blue background (sky/water)
        img = Image.new('RGB', (width, height), color=(173, 216, 230))
        
        # Draw property boundary
        from PIL import ImageDraw
        
        draw = ImageDraw.Draw(img)
        
        # Convert coordinates to pixel positions (simple linear mapping)
        if coordinates and coordinates[0] and len(coordinates[0]) > 2:
            # Get bounding box
            all_coords = coordinates[0]
            lats = [c[0] for c in all_coords]
            lons = [c[1] for c in all_coords]
            
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)
            
            # Add some padding
            lat_padding = (max_lat - min_lat) * 0.1 if max_lat != min_lat else 0.001
            lon_padding = (max_lon - min_lon) * 0.1 if max_lon != min_lon else 0.001
            
            min_lat -= lat_padding
            max_lat += lat_padding
            min_lon -= lon_padding
            max_lon += lon_padding
            
            # Convert to pixel coordinates (simple linear mapping)
            pixel_coords = []
            for lat, lon in all_coords:
                x = int((lon - min_lon) / (max_lon - min_lon) * width) if max_lon != min_lon else width // 2
                y = int((1 - (lat - min_lat) / (max_lat - min_lat)) * height) if max_lat != min_lat else height // 2
                pixel_coords.append((x, y))
            
            # Draw polygon with red outline and semi-transparent fill
            if len(pixel_coords) > 2:
                draw.polygon(pixel_coords, outline='red', fill=None)
                # Draw a second time with fill
                draw.polygon(pixel_coords, fill=(255, 0, 0, 100))
        
        # Save image
        img.save(output_path)
        logger.info("Static map image saved: %s", output_path)
        
        return output_path


def generate_property_map(parcel_data: dict, kml_data: dict, output_dir: str = "images", mode: str = "esri") -> str:
    """
    Generate a property map image
    
    Args:
        parcel_data: Dict with parcel info (lat, lon, address, etc.)
        kml_data: Dict with KML parse results (coordinates, centroid, etc.)
        output_dir: Directory to save the image
        
    Returns:
        Path to generated image
    """
    import os
    os.makedirs(output_dir, exist_ok=True)
    
    parcel_id = parcel_data.get('parcel_id', 'unknown')
    output_path = os.path.join(output_dir, "parcel_%s_map.png" % parcel_id)
    
    # Get center coordinates
    if kml_data and kml_data.get('centroid'):
        center_lat, center_lon = kml_data['centroid']
    elif parcel_data.get('parcel_Latitude') and parcel_data.get('parcel_Longitude'):
        center_lat = float(parcel_data['parcel_Latitude'])
        center_lon = float(parcel_data['parcel_Longitude'])
    else:
        logger.error("No coordinates available for map generation")
        return None
    
    # Get coordinates from KML
    coordinates = kml_data.get('coordinates', []) if kml_data else []
    
    if mode == "esri":
        try:
            return generate_esri_property_map(parcel_data, kml_data, output_path)
        except Exception as e:
            logger.warning("Direct Esri map generation failed: %s", e)
            import traceback
            logger.warning(traceback.format_exc())
            return None

    # Small lots need tighter framing; larger rural tracts need more context.
    fit_max_zoom = _fit_max_zoom_for_acreage(parcel_data.get('parcel_Acreage'))
    generator = MapGenerator(fit_max_zoom=fit_max_zoom)
    
    try:
        # Try folium approach with satellite imagery
        generator.create_map(center_lat, center_lon, coordinates)
        result = generator.save_map(output_path)
        return result
    except Exception as e:
        logger.warning("Folium map generation failed: %s", e)
        import traceback
        logger.warning(traceback.format_exc())
        
        # Fall back to static image
        try:
            result = generator.create_static_map_image(
                center_lat, center_lon,
                coordinates,
                output_path
            )
            return result
        except Exception as e2:
            logger.error("Static map generation also failed: %s", e2)
            return None


def generate_esri_property_map(parcel_data: dict, kml_data: dict, output_path: str, width=2550, height=1782) -> str:
    """Generate a satellite map directly from Esri image services without Chrome."""
    if not PIL_AVAILABLE:
        raise ImportError("PIL/Pillow is required for direct Esri map generation")

    coordinates = kml_data.get('coordinates', []) if kml_data else []
    if not coordinates:
        logger.error("No KML coordinates available for Esri map generation")
        return None

    bbox = _padded_bbox_for_image(coordinates, width, height, parcel_data.get('parcel_Acreage'))
    image = _fetch_esri_export_image(
        'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export',
        bbox,
        width,
        height,
        transparent=False,
    ).convert('RGB')

    _draw_boundary_overlay(image, coordinates, bbox)
    image.save(output_path, quality=95)
    logger.info("Direct Esri map saved: %s", output_path)
    return output_path


def _fetch_esri_export_image(url: str, bbox, width: int, height: int, transparent: bool):
    import requests

    sizes = []
    for candidate_width in (1400, 1200, 900, 700):
        request_width = min(candidate_width, width)
        request_height = max(1, int(round(request_width * height / float(width))))
        size = (request_width, request_height)
        if size not in sizes:
            sizes.append(size)

    last_error = None
    for request_width, request_height in sizes:
        params = {
            'f': 'image',
            'bbox': ','.join(str(x) for x in bbox),
            'bboxSR': '3857',
            'imageSR': '3857',
            'size': '%d,%d' % (request_width, request_height),
            'format': 'png32',
            'transparent': 'true' if transparent else 'false',
        }
        response = requests.get(url, params=params, timeout=45)
        if response.ok:
            image = Image.open(io.BytesIO(response.content))
            if image.size != (width, height):
                image = image.resize((width, height), Image.Resampling.LANCZOS)
            return image

        last_error = requests.HTTPError(
            "%s Server Error: %s" % (response.status_code, response.text[:120]),
            response=response,
        )

    raise last_error


def _padded_bbox_for_image(coordinates, width: int, height: int, acreage) -> Tuple[float, float, float, float]:
    all_coords = [_lonlat_to_web_mercator(lon, lat) for ring in coordinates for lat, lon in ring]
    xs = [x for x, _y in all_coords]
    ys = [y for _x, y in all_coords]

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0

    y_span = max(max_y - min_y, 75.0)
    x_span = max(max_x - min_x, 75.0)
    padding = _bbox_padding_for_acreage(acreage)
    y_span *= 1.0 + padding
    x_span *= 1.0 + padding

    target_ratio = width / float(height)
    current_ratio = x_span / y_span
    if current_ratio < target_ratio:
        x_span = target_ratio * y_span
    else:
        y_span = x_span / target_ratio

    return (
        center_x - x_span / 2.0,
        center_y - y_span / 2.0,
        center_x + x_span / 2.0,
        center_y + y_span / 2.0,
    )


def _bbox_padding_for_acreage(acreage) -> float:
    try:
        acres = float(acreage)
    except (TypeError, ValueError):
        return 0.35

    if acres <= 0.35:
        return 0.80
    if acres <= 1.0:
        return 0.60
    if acres <= 5.0:
        return 0.45
    if acres <= 20.0:
        return 0.35
    return 0.25


def _draw_boundary_overlay(image, coordinates, bbox):
    draw = ImageDraw.Draw(image, 'RGBA')
    min_x, min_y, max_x, max_y = bbox
    width, height = image.size

    def to_pixel(lat, lon):
        point_x, point_y = _lonlat_to_web_mercator(lon, lat)
        pixel_x = int(round((point_x - min_x) / (max_x - min_x) * width))
        pixel_y = int(round((max_y - point_y) / (max_y - min_y) * height))
        return pixel_x, pixel_y

    stroke_width = max(6, width // 360)
    for ring in coordinates:
        points = [to_pixel(lat, lon) for lat, lon in ring]
        if len(points) < 2:
            continue
        if points[0] != points[-1]:
            points.append(points[0])
        if len(points) >= 3:
            draw.polygon(points, fill=(255, 212, 0, 46))
        draw.line(points, fill=(30, 38, 24, 210), width=stroke_width + max(2, stroke_width // 2), joint='curve')
        draw.line(points, fill=(255, 212, 0, 255), width=stroke_width, joint='curve')


def _lonlat_to_web_mercator(lon: float, lat: float) -> Tuple[float, float]:
    origin_shift = 20037508.342789244
    clamped_lat = max(min(float(lat), 85.05112878), -85.05112878)
    x = float(lon) * origin_shift / 180.0
    y = math.log(math.tan((90.0 + clamped_lat) * math.pi / 360.0)) / (math.pi / 180.0)
    y = y * origin_shift / 180.0
    return x, y


def _fit_max_zoom_for_acreage(acreage) -> int:
    """Choose the tightest map fit zoom allowed for the parcel size."""
    try:
        acres = float(acreage)
    except (TypeError, ValueError):
        return 19

    if acres <= 0.35:
        return 20
    if acres <= 1.0:
        return 19
    if acres <= 5.0:
        return 19
    if acres <= 20.0:
        return 18
    return 17


if __name__ == "__main__":
    # Test map generation
    import os
    
    # Sample data
    test_parcel = {
        'parcel_id': '77333122',
        'parcel_Latitude': 36.62019086,
        'parcel_Longitude': -93.60613532,
        'parcel_Parcel Address': '108 S Westview',
        'parcel_City': 'Marionville',
        'parcel_State': 'MO'
    }
    
    # Check if we have KML data
    kml_dir = "kml"
    test_kml_path = os.path.join(kml_dir, "parcel_77333122.kml")
    
    if os.path.exists(test_kml_path):
        from src.kml_parser import parse_kml_file
        kml_data = parse_kml_file(test_kml_path)
        
        output = generate_property_map(test_parcel, kml_data)
        print("Map generated: %s" % output)
    else:
        print("KML file not found: %s" % test_kml_path)
        print("Run scraper first to download KML files")
