"""Memorable names for runs, tasks, findings and submissions (fluent-sun, quiet-otter), as wandb and MLflow give
runs: with the number they make an id that two machines writing at the same time do not both pick."""

from __future__ import annotations

import hashlib
import random

ADJECTIVES = (
    "amber", "ample", "azure", "balmy", "bold", "brave", "breezy", "bright", "brisk", "calm", "candid", "carefree",
    "cheery", "chill", "civil", "clean", "clear", "clever", "cloudy", "comfy", "cool", "cosmic", "cozy", "crisp",
    "curious", "daring", "dashing", "dawn", "deep", "dewy", "dreamy", "dusky", "eager", "earnest", "easy", "elated",
    "electric", "elegant", "epic", "even", "fair", "fancy", "fearless", "fiery", "fine", "firm", "fleet", "fluent",
    "fond", "fresh", "frosty", "gallant", "gentle", "giddy", "glad", "gleaming", "glowing", "golden", "graceful",
    "grand", "great", "green", "happy", "hardy", "hazy", "hearty", "honest", "humble", "icy", "ideal", "jolly",
    "jovial", "keen", "kind", "lazy", "leafy", "light", "lilac", "lively", "lucid", "lucky", "lunar", "lush", "magic",
    "mellow", "merry", "mighty", "mild", "misty", "modest", "mossy", "neat", "nimble", "noble", "olive", "open",
    "pale", "peachy", "plucky", "polar", "polite", "proud", "quick", "quiet", "radiant", "rapid", "rare", "ready",
    "regal", "rosy", "royal", "rustic", "sage", "salty", "sandy", "serene", "sharp", "shiny", "silent", "silky",
    "silver", "sleek", "smart", "smooth", "snowy", "solar", "solid", "sonic", "sparkling", "spry", "steady", "stellar",
    "stoic", "stormy", "sturdy", "sunny", "super", "sweet", "swift", "tidy", "tranquil", "true", "twilight", "upbeat",
    "valiant", "vast", "velvet", "vibrant", "vivid", "warm", "wild", "windy", "wise", "witty", "woven", "young",
    "zany", "zealous", "zesty",
)
NOUNS = (
    "acorn", "aurora", "badger", "bamboo", "beacon", "bear", "bee", "birch", "bison", "blossom", "breeze", "brook",
    "butterfly", "canyon", "cedar", "cherry", "cliff", "cloud", "comet", "coral", "cosmos", "crane", "creek", "cricket",
    "dawn", "deer", "delta", "dew", "dolphin", "dove", "dragon", "dream", "dune", "eagle", "echo", "elm", "ember",
    "falcon", "fern", "field", "finch", "firefly", "flame", "flower", "fog", "forest", "fox", "frog", "frost", "galaxy",
    "gazelle", "glade", "glacier", "grass", "grove", "gull", "harbor", "hawk", "haze", "heron", "hill", "horizon",
    "iris", "island", "ivy", "jaguar", "jay", "koala", "lagoon", "lake", "lark", "leaf", "lemur", "lily", "lion",
    "lotus", "lynx", "maple", "marsh", "meadow", "meteor", "mist", "moon", "moss", "moth", "mountain", "nebula",
    "night", "oak", "ocean", "orbit", "orca", "otter", "owl", "panda", "panther", "pebble", "pelican", "penguin",
    "pine", "planet", "plateau", "pond", "poppy", "prairie", "puffin", "quasar", "rabbit", "rain", "raven", "reef",
    "ridge", "river", "robin", "rose", "sage", "salmon", "sea", "seal", "shadow", "shore", "sky", "snow", "sparrow",
    "spring", "spruce", "star", "stone", "storm", "stream", "summit", "sun", "sunset", "swan", "thunder", "tide",
    "tiger", "trail", "tree", "tulip", "valley", "violet", "voice", "wave", "whale", "willow", "wind", "wolf", "wood",
    "wren", "yak", "zebra", "zephyr",
)


def fresh(used=(), rng: random.Random | None = None) -> str:
    """A name not in `used`."""
    rng = rng or random.SystemRandom()
    used = set(used)
    for _ in range(1000):
        name = f"{rng.choice(ADJECTIVES)}-{rng.choice(NOUNS)}"
        if name not in used:
            return name
    while (name := f"{rng.choice(ADJECTIVES)}-{rng.choice(ADJECTIVES)}-{rng.choice(NOUNS)}") in used:
        pass
    return name


def stable(seed: str, used=()) -> str:
    """The same name for the same seed (an imported row imported again), avoiding `used`."""
    return fresh(used, random.Random(hashlib.sha256(seed.encode()).hexdigest()))
