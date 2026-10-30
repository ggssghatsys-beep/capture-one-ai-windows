"""Opt in to temporary password-protected Pilot sharing for one approved collection.

Each native preview starts the inactive mobile server, disables remote writes,
reads through loopback, then stops/restores sharing settings. C1 also listens on
the LAN. The configured random password replaces the prior write-only password.
"""
import argparse
import json
import secrets
from c1ai.macos_backend import MacOSBackend, SUPPORTED_VERSION
from c1ai.macos_ipc import state_directory
from c1ai.macos_pilot import cleanup, control, save_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-pilot-sharing", action="store_true", help="Explicitly accept temporary LAN sharing and password replacement")
    parser.add_argument("--collection-id")
    parser.add_argument("--selected-in-current-collection", action="store_true",
                        help="Permit each selected photo in the current collection; share only that RAW in a temporary album")
    parser.add_argument("--cleanup", action="store_true", help="Stop only a previously recorded preview service; never replay a photo operation")
    args = parser.parse_args()
    root = state_directory()
    if args.cleanup:
        print(json.dumps(cleanup(root)))
        return
    if not args.enable_pilot_sharing or bool(args.collection_id) == args.selected_in_current_collection:
        parser.error("Use --enable-pilot-sharing with either --collection-id ID or --selected-in-current-collection")
    target = MacOSBackend(root).observe([])["target"]
    if target["instance"]["version"] != SUPPORTED_VERSION:
        parser.error("Capture One version has not been qualified")
    config = {"capture_one_version": SUPPORTED_VERSION, "document": target["document"],
              "password": secrets.token_urlsafe(32)}
    # Do not expand photo sharing implicitly. Only the selected RAW and its
    # variants may be present; the runtime rechecks this before every startup.
    if args.selected_in_current_collection:
        state = control("scope", {"target": target})
        config.update(version=2, scope="selected_photo_in_collection", source_collection=state["collection"])
    else:
        config.update(version=1, collection_id=args.collection_id,
                      images={target["image"]["id"]: target["image"]["path"]})
        state = control("inspect", {"target": target, "collection_id": args.collection_id,
                                    "images": config["images"]})
    if state["settings"]["mobile"] or state["settings"]["web"]:
        parser.error("Stop existing Pilot sharing first")
    save_private(root / "macos-pilot.json", config)
    print(json.dumps({"configured": True, "service_started": False,
                      "scope": "selected RAW and its variants in the approved collection"}))


if __name__ == "__main__":
    main()
