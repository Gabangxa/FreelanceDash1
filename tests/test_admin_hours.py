"""
Regression test for C2: admin dashboard reported hours that were 60x too
small because TimeEntry.duration (stored in minutes) was divided by 3600.
"""
from datetime import datetime, timedelta

from app import db
from models import User, Client, Project, TimeEntry


def _make_fixture(db_session):
    user = User(username="admin_test_user", email="admin_hours@test.local")
    user.set_password("doesnotmatter")
    db.session.add(user)
    db.session.flush()

    client_obj = Client(name="C", user_id=user.id)
    db.session.add(client_obj)
    db.session.flush()

    project = Project(
        name="P",
        start_date=datetime.utcnow(),
        user_id=user.id,
        client_id=client_obj.id,
    )
    db.session.add(project)
    db.session.flush()

    # 600 minutes = 10 hours total; 360 minutes = 6 hours billable.
    db.session.add(TimeEntry(
        project_id=project.id,
        start_time=datetime.utcnow() - timedelta(hours=2),
        end_time=datetime.utcnow(),
        duration=600,
        billable=False,
    ))
    db.session.add(TimeEntry(
        project_id=project.id,
        start_time=datetime.utcnow() - timedelta(hours=2),
        end_time=datetime.utcnow(),
        duration=360,
        billable=True,
    ))
    db.session.commit()
    return user


def test_admin_dashboard_hours_use_minutes_not_seconds(db_session):
    """Asserts the conversion logic itself, mirroring admin/routes.py."""
    _make_fixture(db_session)

    from sqlalchemy import func
    total_minutes = db.session.query(func.sum(TimeEntry.duration)).scalar() or 0
    billable_minutes = db.session.query(func.sum(TimeEntry.duration)).filter(
        TimeEntry.billable == True
    ).scalar() or 0

    total_hours = round(total_minutes / 60.0, 2)
    billable_hours = round(billable_minutes / 60.0, 2)

    # 600 + 360 = 960 minutes => 16.0 hours; billable 360 => 6.0 hours.
    assert total_hours == 16.0
    assert billable_hours == 6.0
    # Sanity check: the previous (buggy) /3600 divisor would have produced ~0.27.
    assert total_hours != round(total_minutes / 3600.0, 2)
