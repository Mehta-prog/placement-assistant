from sqlalchemy import Column, Integer, String
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String, default="")
    google_sub = Column(String, unique=True, index=True, nullable=True)
    password = Column(String, default="")

    selected_template = Column(String, default="classic_ats")
    preferred_location = Column(String, default="")
    preferred_lat = Column(String, default="")
    preferred_lng = Column(String, default="")


class SavedJob(Base):
    __tablename__ = "saved_jobs"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, index=True)  # stores email
    title = Column(String)
    company = Column(String)
    location = Column(String)
    url = Column(String, index=True)
    status = Column(String, default="saved")