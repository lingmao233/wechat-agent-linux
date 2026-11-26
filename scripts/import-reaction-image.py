"""Register an already-approved picture as a reusable reaction-image asset."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from wechat_agent.config import Settings
from wechat_agent.reaction_images import ReactionImagePlan, ReactionImages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="picture to copy into the cache")
    parser.add_argument("--config", type=Path, default=Path(".env"))
    parser.add_argument("--emotion", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--prompt", required=True, help="visual description stored in the sidecar")
    parser.add_argument("--caption", default="", help="the only words printed on the picture")
    parser.add_argument("--scene", default="", help="when this picture fits; required for reuse")
    parser.add_argument("--tags", default="", help="comma separated topic tags")
    args = parser.parse_args()

    settings = Settings(_env_file=args.config)
    cache = ReactionImages(settings, image_client=None)
    plan = ReactionImagePlan(
        emotion=args.emotion,
        action=args.action,
        prompt=args.prompt,
        caption=args.caption,
        scene=args.scene,
        topic_tags=tuple(tag.strip() for tag in args.tags.split(",") if tag.strip()),
    )
    asset = cache.register(args.image, plan)
    report = {
        "image": str(args.image),
        "style_id": cache.root.name if cache.image_style else "",
        "registered": asset is not None,
        "image_id": asset.image_id if asset else "",
        "reused": asset.reused if asset else False,
        "path": asset.path if asset else "",
        "catalog": cache.catalog(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if asset else 1


if __name__ == "__main__":
    raise SystemExit(main())
