from MySQL import MySQL

from traceback import format_exc
from json import dumps as json_dumps, loads as json_loads, dump as json_dump, load as json_load
# from time import sleep
# from os import remove as os_remove
from os.path import exists as os_exists
import datetime

from nts_parser import NetSchoolUser


DOCPATH = "doctmp"

PROCESS_KILL_TIMOUT = datetime.timedelta(minutes=10)
SIM_RUNNING = 5


def get_monday(day=None):
    if day is None:
        day = datetime.datetime.today()
    result = day - datetime.timedelta(days=day.weekday())
    return (result.date() if isinstance(result, datetime.datetime) else result)


def get_update_timeout(person):
    def fun(x):
        return (x ** 2) / 2 + 1

    if person["last_visit"] is None:
        return datetime.timedelta(), datetime.timedelta()

    update_timeout = fun((datetime.datetime.now() - person["last_visit"]).seconds / 86400)
    return datetime.timedelta(hours=update_timeout / 12), datetime.timedelta(hours=update_timeout)


def week_period(day_start, day_end):
    day_start = get_monday(day_start)
    day_end = get_monday(day_end)

    for i in range((day_end - day_start).days // 7):
        yield day_start + datetime.timedelta(weeks=i)


def day_period(day_start, day_end):
    for i in range((day_end - day_start).days):
        yield day_start + datetime.timedelta(days=i)


# def school_year_days(year=None):
#     if year is None:
#         year = datetime.datetime.today().year
#         if datetime.datetime.today().month < 9:
#             year -= 1

#     yield from day_period(
#         datetime.date(year, 9, 1),
#         datetime.date(year + 1, 6, 1)
#     )


def school_year_weeks(year=None):
    if year is None:
        year = datetime.datetime.today().year
        if datetime.datetime.today().month < 9:
            year -= 1

    yield from week_period(
        datetime.date(year, 9, 1),
        datetime.date(year + 1, 6, 1)
    )


# def school_year_weeks_from_now(year=None):
#     if year is None:
#         year = datetime.datetime.today().year
#         if datetime.datetime.today().month < 9:
#             year -= 1

#     yield from week_period(
#         get_monday(),
#         datetime.date(year + 1, 6, 1)
#     )


def get_full_weekly_timetable(nts, monday):
    # monday = get_monday(monday)  # If monday is actually not monday

    result = {}

    try:
        weekly_timetable = nts.get_weekly_timetable_ext(date=monday)

        for day in weekly_timetable:

            # Be careful not to ruin the lesson order
            weekly_timetable[day].sort(key=lambda item: 0 if item["type"] == "lesson" else 1 if item["type"] == "vacation" else 2)

            for item in weekly_timetable[day]:
                if "start" in item:
                    item["start"] = item["start"].strftime("%Y-%m-%d %H:%M:%S")

                if "end" in item:
                    item["end"] = item["end"].strftime("%Y-%m-%d %H:%M:%S")

            # Remove None at the end of lessons
            i = len(weekly_timetable[day]) - 1
            while i >= 0 and weekly_timetable[day][i]["type"] != "lesson":
                i -= 1
            while i >= 0 and weekly_timetable[day][i]["name"] is None:
                del weekly_timetable[day][i]
                i -= 1

            result[day.strftime("%Y-%m-%d")] = [
                [
                    item["type"],
                    item["name"],
                    item["start"] if "start" in item else None,
                    item["end"] if "end" in item else None
                ]
                for item in weekly_timetable[day]
            ]

    except Exception:
        print(format_exc())

        for day in day_period(monday, monday + datetime.timedelta(days=7)):
            result[day.strftime("%Y-%m-%d")] = None

    return result


def run_person(mysql, person):

    fast_update = person["last_update"] is None
    ordinary_update = person["last_update"] is None or datetime.datetime.now() - person["last_update"] > get_update_timeout(person)[0]
    full_update = person["last_full_update"] is None or datetime.datetime.now() - person["last_full_update"] > get_update_timeout(person)[1]

    if not (fast_update or ordinary_update or full_update):
        return

    print("Running \"{}\" for person {}...".format(
        "fast_update" if fast_update else "full_update" if full_update else "ordinary_update",
        person["username"]
    ))

    nts = NetSchoolUser(person["username"], person["password"], DOCPATH, "config.json")

    try:
        login_status = nts.login()

        if login_status:
            print("Login success")

            # Announcements:
            try:
                if not fast_update:
                    print("Getting announcements...")
                    announcements = nts.get_announcements()

                    mysql.query("LOCK TABLES announcements WRITE;TRUNCATE TABLE `announcements`")

                    for author, title, date, text in announcements:
                        mysql.query(
                            "INSERT INTO `announcements` (`author`, `title`, `date`, `text`) VALUES (%s, %s, %s, %s)",
                            (author, title, date, text)
                        )

                    mysql.query("UNLOCK TABLES;")
                    mysql.commit()

            except Exception:
                print(format_exc())

            # Timetable:
            try:
                timetable = {}

                if fast_update:
                    cur_period = week_period(get_monday(), get_monday() + datetime.timedelta(days=7))

                elif full_update:
                    cur_period = school_year_weeks()

                else:
                    try:
                        timetable = {
                            date: value for date, value in (
                                json_loads(mysql.fetch("SELECT `timetable` FROM `users` WHERE `id` = %s", format(person["id"]))[0]["timetable"]).items()
                            ) if datetime.datetime.strptime(date, "%Y-%m-%d").date() < get_monday()
                        }
                        cur_period = [
                            get_monday() - datetime.timedelta(weeks=1),
                            get_monday(),
                            get_monday() + datetime.timedelta(weeks=1)
                        ]

                    except Exception:
                        print(format_exc())
                        timetable = {}
                        cur_period = school_year_weeks()

                for week_start in cur_period:
                    print("Getting timetable for week starting with {}...".format(week_start))

                    weekly_timetable = get_full_weekly_timetable(nts, week_start)

                    timetable.update(**weekly_timetable)

                mysql.query("UPDATE `users` SET `timetable` = %s WHERE `id` = %s", (json_dumps(timetable, ensure_ascii=False), person["id"]))

            except Exception:
                print(format_exc())

            # Diary:
            try:
                diary = {}

                if fast_update:
                    cur_period = []

                elif full_update:
                    cur_period = school_year_weeks()

                else:
                    try:
                        diary = {
                            date: value for date, value in (
                                json_loads(mysql.fetch("SELECT `diary` FROM `users` WHERE `id` = %s", format(person["id"]))[0]["diary"]).items()
                            ) if datetime.datetime.strptime(date, "%Y-%m-%d").date() < get_monday()
                        }
                        cur_period = [
                            get_monday() - datetime.timedelta(weeks=1),
                            get_monday(),
                            get_monday() + datetime.timedelta(weeks=1)
                        ]

                    except Exception:
                        print(format_exc())
                        diary = {}
                        cur_period = school_year_weeks()

                for week_start in cur_period:
                    print("Getting diary for week starting with {}...".format(week_start))

                    weekly_diary = nts.get_diary(week_start, full=True)

                    diary.update(**{key.strftime("%Y-%m-%d"): weekly_diary[key] for key in weekly_diary})

                mysql.query("UPDATE `users` SET `diary` = %s WHERE `id` = %s", (json_dumps(diary, ensure_ascii=False), person["id"]))

            except Exception:
                print(format_exc())

            # ==============================| UPLOADING |==============================

            print("Uploading...")

            if nts.name is not None:
                mysql.query("UPDATE `users` SET `name` = %s WHERE `id` = %s", (
                    ' '.join(nts.name.split()[::-1]) if len(nts.name.split()) == 2 else nts.name,
                    person["id"]
                ))

            if nts.class_ is not None:
                mysql.query("UPDATE `users` SET `class` = %s WHERE `id` = %s", (
                    nts.class_,
                    person["id"]
                ))

            mysql.query("UPDATE `users` SET `last_update` = %s, `mail` = %s WHERE `id` = %s", (

                (datetime.datetime.now() - datetime.timedelta(hours=8760)).strftime("%Y-%m-%d %H:%M:%S")
                if person["last_update"] is None else
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),

                nts.mail,

                person["id"]
            ))

            if full_update and not fast_update:
                mysql.query("UPDATE `users` SET `last_full_update` = %s WHERE `id` = %s", (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    person["id"]
                ))

        elif login_status is not None:  # if login_status == False
            print("Login failed")
            mysql.query("DELETE FROM `users` WHERE `id` = %s", (person["id"],))

    except Exception:
        print(format_exc())

    finally:
        print("Logout")
        mysql.commit()
        del nts

    print()


def run_last():

    def get_cur_running():
        try:
            with open(".run_lock.json", 'r', encoding="utf-8") as file:
                cur_running = json_load(file)
        except Exception:
            cur_running = {}

        return {
            int(key): float(value)
            for key, value in cur_running.items()
            if datetime.datetime.now() - datetime.datetime.fromtimestamp(float(value)) <= PROCESS_KILL_TIMOUT
        }

    def set_cur_running(cur_running):
        with open(".run_lock.json", 'w', encoding="utf-8") as file:
            json_dump(cur_running, file, ensure_ascii=False)

    if not os_exists(".run_lock.json"):
        with open(".run_lock.json", 'w') as file:
            file.write(json_dumps({}))

    mysql = None
    try:
        mysql = MySQL("config.json")

        cur_running = get_cur_running()

        if len(cur_running) < SIM_RUNNING:

            person = mysql.fetch(
                """
                    SELECT * FROM `users` WHERE

                    (
                        `last_visit` IS NULL OR

                        UNIX_TIMESTAMP(`last_update`) + (

                            POW((UNIX_TIMESTAMP() - UNIX_TIMESTAMP(`last_visit`)) / 86400, 2) / 2 + 1

                        ) / 12 * 3600 < UNIX_TIMESTAMP(NOW())
                    )

                    {} ORDER BY

                    UNIX_TIMESTAMP(`last_update`) + (

                        POW((UNIX_TIMESTAMP() - UNIX_TIMESTAMP(`last_visit`)) / 86400, 2) / 2 + 1

                    ) / 12 * 3600

                    ASC LIMIT 1
                """.format(
                    (
                        "AND " + " AND ".join("`id` != '{}'".format(user_id) for user_id in cur_running)
                    )
                    if cur_running else ""
                )
            )

            if person:
                user_id = person[0]['id']
                cur_running[user_id] = datetime.datetime.now().timestamp()
                set_cur_running(cur_running)

                try:
                    run_person(mysql, person[0])
                except Exception:
                    print(format_exc())

                cur_running = get_cur_running()

                if user_id in cur_running:
                    del cur_running[user_id]

                set_cur_running(cur_running)

    except Exception:
        print(format_exc())
        # sleep(10)

    finally:
        del mysql


if __name__ == "__main__":
    run_last()
