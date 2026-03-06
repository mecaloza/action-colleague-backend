"""Seed the database with sample data."""

import json
from datetime import date, datetime

from auth import hash_password
from database import Base, SessionLocal, engine
from models import (
    Certificate,
    Course,
    Document,
    Enrollment,
    Evaluation,
    Module,
    ModuleProgress,
    User,
)


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # Clear existing data
    for model in [Certificate, ModuleProgress, Document, Enrollment, Evaluation, Module, Course, User]:
        db.query(model).delete()
    db.commit()

    # ── Users (with hierarchy) ─────────────────────────────────────────
    admin = User(
        name="Admin",
        email="admin@actioncolleague.com",
        password_hash=hash_password("admin123"),
        role="admin",
        position="CEO",
        department="Executive",
        hire_date=date(2019, 1, 1),
    )
    db.add(admin)
    db.flush()

    vp_eng = User(
        name="Laura Sánchez",
        email="laura.sanchez@actioncolleague.com",
        password_hash=hash_password("password123"),
        role="admin",
        position="VP Engineering",
        department="Engineering",
        reports_to=admin.id,
        hire_date=date(2019, 6, 1),
    )
    hr_director = User(
        name="Ana García",
        email="ana.garcia@actioncolleague.com",
        password_hash=hash_password("password123"),
        role="admin",
        position="HR Director",
        department="Human Resources",
        reports_to=admin.id,
        hire_date=date(2020, 3, 15),
    )
    db.add_all([vp_eng, hr_director])
    db.flush()

    dev1 = User(
        name="Carlos López",
        email="carlos.lopez@actioncolleague.com",
        password_hash=hash_password("password123"),
        role="collaborator",
        position="Software Engineer",
        department="Engineering",
        reports_to=vp_eng.id,
        hire_date=date(2022, 7, 1),
    )
    dev2 = User(
        name="María Rodríguez",
        email="maria.rodriguez@actioncolleague.com",
        password_hash=hash_password("password123"),
        role="collaborator",
        position="Frontend Developer",
        department="Engineering",
        reports_to=vp_eng.id,
        hire_date=date(2023, 1, 10),
    )
    accountant = User(
        name="Pedro Martínez",
        email="pedro.martinez@actioncolleague.com",
        password_hash=hash_password("password123"),
        role="collaborator",
        position="Accountant",
        department="Finance",
        reports_to=admin.id,
        hire_date=date(2021, 11, 20),
    )
    db.add_all([dev1, dev2, accountant])
    db.flush()

    users = [admin, vp_eng, hr_director, dev1, dev2, accountant]

    # ── Courses ───────────────────────────────────────────────────────
    courses = [
        Course(
            title="Onboarding: Company Culture",
            description="Introduction to our company values, mission, and work culture.",
            status="published",
            created_by=hr_director.id,
        ),
        Course(
            title="Cybersecurity Fundamentals",
            description="Learn the basics of cybersecurity and best practices for staying safe online.",
            status="published",
            created_by=vp_eng.id,
        ),
        Course(
            title="Leadership & Management",
            description="Develop leadership skills for managing teams effectively.",
            status="draft",
            created_by=hr_director.id,
        ),
    ]
    db.add_all(courses)
    db.flush()

    # ── Modules ───────────────────────────────────────────────────────
    modules_data = [
        Module(course_id=courses[0].id, title="Welcome & Overview", order=1,
               content_text="Welcome to Action Colleague! In this module you will learn about our history and mission."),
        Module(course_id=courses[0].id, title="Company Values", order=2,
               content_text="Our core values: Innovation, Integrity, Collaboration, and Excellence."),
        Module(course_id=courses[0].id, title="Tools & Systems", order=3,
               content_text="Overview of the tools and systems you will use daily."),
        Module(course_id=courses[1].id, title="Password Security", order=1,
               content_text="Learn how to create and manage strong passwords."),
        Module(course_id=courses[1].id, title="Phishing Awareness", order=2,
               content_text="How to identify and avoid phishing attacks."),
    ]
    db.add_all(modules_data)
    db.flush()

    # ── Evaluations ───────────────────────────────────────────────────
    evals = [
        Evaluation(module_id=modules_data[0].id, questions_json=json.dumps([
            {"question": "What year was the company founded?", "options": ["2010", "2015", "2018", "2019"], "correct": 3},
            {"question": "What is our mission?", "options": ["Profit", "Innovation for all", "Growth", "Speed"], "correct": 1},
        ])),
        Evaluation(module_id=modules_data[3].id, questions_json=json.dumps([
            {"question": "Minimum password length recommended?", "options": ["6", "8", "12", "4"], "correct": 2},
            {"question": "Should you reuse passwords?", "options": ["Yes", "No", "Sometimes", "Only for social media"], "correct": 1},
        ])),
    ]
    db.add_all(evals)
    db.flush()

    # ── Enrollments ───────────────────────────────────────────────────
    enrollments = [
        Enrollment(user_id=dev1.id, course_id=courses[0].id, status="completed", progress_pct=100.0),
        Enrollment(user_id=dev1.id, course_id=courses[1].id, status="in_progress", progress_pct=50.0),
        Enrollment(user_id=dev2.id, course_id=courses[0].id, status="in_progress", progress_pct=33.3),
        Enrollment(user_id=accountant.id, course_id=courses[0].id, status="assigned", progress_pct=0.0),
    ]
    db.add_all(enrollments)
    db.flush()

    # ── Module Progress ───────────────────────────────────────────────
    progress = [
        ModuleProgress(enrollment_id=enrollments[0].id, module_id=modules_data[0].id, completed=True, score=90.0, completed_at=datetime(2024, 2, 10)),
        ModuleProgress(enrollment_id=enrollments[0].id, module_id=modules_data[1].id, completed=True, score=85.0, completed_at=datetime(2024, 2, 12)),
        ModuleProgress(enrollment_id=enrollments[0].id, module_id=modules_data[2].id, completed=True, score=95.0, completed_at=datetime(2024, 2, 15)),
        ModuleProgress(enrollment_id=enrollments[1].id, module_id=modules_data[3].id, completed=True, score=80.0, completed_at=datetime(2024, 3, 1)),
    ]
    db.add_all(progress)
    db.flush()

    # ── Certificates ──────────────────────────────────────────────────
    certs = [
        Certificate(enrollment_id=enrollments[0].id, pdf_url="/static/certs/cert_1.pdf"),
    ]
    db.add_all(certs)
    db.flush()

    # ── Documents ─────────────────────────────────────────────────────
    doc = Document(
        user_id=dev1.id,
        type="labor_letter",
        pdf_url="/static/documents/labor_letter_2.pdf",
    )
    doc.template_data = {
        "user_name": dev1.name,
        "position": dev1.position,
        "department": dev1.department,
        "hire_date": str(dev1.hire_date),
    }
    db.add(doc)

    db.commit()
    db.close()
    print("Seed data loaded successfully.")
    print(f"  Admin: admin@actioncolleague.com / admin123")
    print(f"  Users: 6 total (1 admin CEO + 2 managers + 3 employees)")
    print(f"  Hierarchy: CEO -> VP Eng, HR Director, Accountant; VP Eng -> 2 devs")


if __name__ == "__main__":
    seed()
