"""Surface classes for Enfusion masks."""

SURFACE_CLASSES: dict[str, dict] = {
    "asphalt":      {"color": 128, "description": "Paved roads and urban areas"},
    "gravel":       {"color": 160, "description": "Gravel roads and paths"},
    "crop":         {"color": 145, "description": "Agricultural land / crop fields"},
    "dirt":         {"color": 140, "description": "Dirt roads and paths"},
    "grass":        {"color": 100, "description": "Grassland and meadows"},
    "forest_floor": {"color":  80, "description": "Forest floor - deciduous"},
    "pine_floor":   {"color":  70, "description": "Forest floor - coniferous"},
    "rock":         {"color": 200, "description": "Exposed rock and steep slopes"},
    "sand":         {"color": 220, "description": "Sandy areas and beaches"},
    "water_edge":   {"color": 180, "description": "Near-water transition zone"},
}
