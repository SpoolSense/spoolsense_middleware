import logging
from state.models import SpoolInfo, SpoolAssignment
from state.moonraker_db import MoonrakerDB

# Set up basic logging so we can see what happens
logging.basicConfig(level=logging.INFO)

# CHANGE THIS to your printer's IP if you are running this from your PC
# If running directly on the Pi, "http://localhost:7125" is perfect.
MOONRAKER_URL = "http://localhost:7125" 

def run_test():
    db = MoonrakerDB(MOONRAKER_URL)

    # 1. Create a fake OpenPrintTag spool
    fake_spool = SpoolInfo(
        spool_uid="04AABBCCDD11",
        source="openprinttag",
        brand="Prusament",
        material_type="PETG",
        color_hex="#1A1A1A",
        remaining_weight_g=640.5
    )

    # 2. Create a fake assignment (e.g., putting it in AFC Lane 3)
    fake_assignment = SpoolAssignment(
        target_type="afc_lane",
        target_id="lane3",
        spool_uid=fake_spool.spool_uid,
        active=True
    )

    # 3. Save them to Moonraker
    print("Saving spool...")
    db.save_spool(fake_spool)

    print("Saving assignment...")
    db.save_assignment(fake_assignment)
    
    print("\nDone! Check your browser to verify.")

if __name__ == "__main__":
    run_test()