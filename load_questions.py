# load_questions.py
import re
from pathlib import Path
import motor.motor_asyncio
import asyncio

# 1) Paste your full MongoDB Atlas URI here
MONGO_URI = "mongodb+srv://chatbotuser:chatbot123@cluster0.cnsqe8t.mongodb.net/?appName=Cluster0"

# 2) MongoDB client and collection
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client["cyber_training"]
questions_collection = db["questions"]

async def main():
    print("SCRIPT STARTED ✅")
    print("🔍 Reading questions file...")

    # Path to the questions file (must be in the project root)
    txt_path = Path("MostAskedQ&A.txt")

    if not txt_path.exists():
        raise FileNotFoundError("❌ MostAskedQ&A.txt not found in the project root directory.")

    # Read text file
    text = txt_path.read_text(encoding="utf-8").strip()

    # Add a leading newline to simplify regex splitting
    text = "\n" + text

    # Split into blocks by numbered pattern: 1., 2., 3., ...
    blocks = re.split(r"\n\s*(?=\d+\.\s)", text)

    docs = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.splitlines()
        first_line = lines[0].strip()

        # Match "number. question text"
        match = re.match(r"(\d+)\.\s*(.+)", first_line)
        if not match:
            continue

        question_number = int(match.group(1))
        question_text = match.group(2).strip()

        # Remaining lines are the answer
        answer_text = " ".join(
            line.strip() for line in lines[1:] if line.strip()
        )

        doc = {
            "number": question_number,
            "question_text": question_text,
            "answer_text": answer_text,
            "level": 1,          # default level
            "module": "general"  # default module
        }

        docs.append(doc)

    print(f"📄 Detected {len(docs)} questions.")

    if docs:
        print("🗑 Clearing previous documents from 'questions' collection...")
        await questions_collection.delete_many({})

        print("⬆️ Inserting questions into MongoDB Atlas...")
        result = await questions_collection.insert_many(docs)

        print(f"✅ Done. Inserted {len(result.inserted_ids)} questions.")
    else:
        print("⚠️ No questions detected. Please check the file format.")

# 👇 THIS PART IS CRUCIAL: without this, nothing runs when you call `python load_questions.py`
if __name__ == "__main__":
    asyncio.run(main())
