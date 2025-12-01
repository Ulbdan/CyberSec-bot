# app/db.py
import motor.motor_asyncio

# Use the SAME URI you used in load_questions.py
MONGO_URI = "mongodb+srv://chatbotuser:chatbot123@cluster0.cnsqe8t.mongodb.net/?appName=Cluster0"

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client["cyber_training"]

# Collections
users_collection = db["users"]
questions_collection = db["questions"]
