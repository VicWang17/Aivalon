import sys
import os
import asyncio
import logging

# Add backend to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), "../"))

from app.db.base import SessionLocal
from app.models.outbox import OutboxEvent
from app.services.game_service import GameService
from app.core.outbox_relay import process_outbox_events

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_outbox")

async def main():
    logger.info("Starting Outbox Flow Test")
    
    # 1. Create a game (simulating user action)
    player_ids = [1, 2, 3, 4, 5, 6, 7, 8]
    user_map = {i: f"User_{i}" for i in player_ids}
    
    logger.info("Creating game...")
    try:
        game_state = await GameService.create_game(player_ids, user_map, creator_id=1)
        game_id = game_state.game_id
        logger.info(f"Game created: {game_id}")
    except Exception as e:
        logger.error(f"Failed to create game: {e}")
        return

    # 2. Check Outbox (Pending)
    db = SessionLocal()
    pending_count = 0
    try:
        pending_events = db.query(OutboxEvent).filter(
            OutboxEvent.aggregate_id == game_id,
            OutboxEvent.status == "pending"
        ).all()
        
        pending_count = len(pending_events)
        logger.info(f"Pending events count: {pending_count}")
        if pending_count == 0:
            logger.error("No pending events found! Outbox write failed.")
            return
            
        for event in pending_events:
            logger.info(f"Found pending event: {event.event_type}")
            
    finally:
        db.close()

    # 3. Run Relay
    logger.info("Running Relay...")
    # process_outbox_events()
    # It might process other events too, but for test we only care about count.
    # Note: process_outbox_events limits to 50.
    relayed_count = process_outbox_events()
    logger.info(f"Relayed {relayed_count} events.")
    
    # 4. Check Outbox (Processed)
    db = SessionLocal()
    try:
        processed_events = db.query(OutboxEvent).filter(
            OutboxEvent.aggregate_id == game_id,
            OutboxEvent.status == "processed"
        ).all()
        
        logger.info(f"Processed events count: {len(processed_events)}")
        if len(processed_events) >= pending_count:
             logger.info("SUCCESS: All events processed.")
        else:
             logger.error("FAILURE: Some events not processed.")
             
    finally:
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
