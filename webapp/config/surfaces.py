"""Surface classes for Enfusion masks."""

SURFACE_CLASSES: dict[str, dict] = {
    "asphalt":      {"color": 128, "description": "Paved roads and urban areas"},
    "gravel":       {"color": 160, "description": "Gravel roads and paths"},
    "dirt":         {"color": 140, "description": "Dirt paths and plowed fields"},
    "grass":        {"color": 100, "description": "Grassland and meadows"},
    "forest_floor": {"color":  80, "description": "Forest floor - deciduous"},
    "pine_floor":   {"color":  70, "description": "Forest floor - coniferous"},
    "rock":         {"color": 200, "description": "Exposed rock and steep slopes"},
    "sand":         {"color": 220, "description": "Sandy areas and beaches"},
    "water_edge":   {"color": 180, "description": "Near-water transition zone"},
}
