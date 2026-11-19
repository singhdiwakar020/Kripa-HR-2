"""Builds sample_career_form.xlsx with the messy cases from the flow document."""
import random
from datetime import datetime

from openpyxl import Workbook

random.seed(7)
wb = Workbook()
ws = wb.active
ws.title = "Career Form"
ws.append(["Name", "Resume Link", "Email", "Phone", "Experience", "Company", "Location", "AI Screening Score",
           "Best Match Role", "Screening Reasoning", "Status", "Communication", "Experience", "Notice Period",
           "Ready To Relocate", "Salary In Hand", "Expectation"])
cities = ["Bhopal", "Indore", "Pune", "Delhi", "Bengaluru", "Jabalpur", "Mumbai"]
roles = ["AI Developer Intern"] * 18 + ["AI Developer"] * 3 + ["Digital Marketing Intern"] * 2 + ["Sales Intern"]
skills = ["Python, FastAPI, RAG", "Python, PyTorch", "LangChain, OpenAI APIs", "React, Node", "SEO, content", "Java, Spring Boot"]
names = ["Rahul", "Anita", "Vikram", "Neha K.", "Amit", "Priya", "Karan", "Sneha", "Dev", "Isha", "Mohit", "Riya",
         "Arjun", "Pooja", "Sahil", "Tanvi", "Yash", "Kavya", "Nikhil", "Meera", "Rohan", "Divya", "Aman", "Simran"]
statuses = ["New"] * 20 + ["Contacted", "Not Responded", "Rejected", "Assignment Submitted"]
for i, n in enumerate(names):
    phone = random.choice([f"+91 9{random.randint(100000000, 999999999)}", f"9{random.randint(100000000, 999999999)}",
                           f"(+91) 9{random.randint(100000000, 999999999)}"])
    exp = random.choice(["Fresher", "6 months", "1 yr 3 mo [Full stack, 1 internship]", "2 years", "3 years"])
    ws.append([f"{n} Sharma", f"https://drive.example.com/{i}", f"{n.lower().replace(' ', '').replace('.', '')}{i}@mail.com",
               phone, exp, random.choice(["", "TCS", "Infosys", "Startup X"]), random.choice(cities),
               round(random.uniform(3.5, 9.5), 1), roles[i % len(roles)],
               f"Skills: {random.choice(skills)}. Good project work." if i != 5 else "No text found in resume",
               statuses[i % len(statuses)], "", "", random.choice(["", "30 days", "Immediate", "60 days"]),
               random.choice(["", "Yes", "No"]), "", ""])
# duplicate applicant (same email + same phone different format)
ws.append(["Rahul Sharma", "https://drive.example.com/dup", "rahul0@mail.com", "09999999999", "Fresher", "", "Bhopal",
           8.0, "AI Developer Intern", "Duplicate application", "New", "", "", "", "", "", ""])
# communication rating damaged into a date + row with no contact details
ws.append(["Date Damaged", "https://drive.example.com/x", "dd@mail.com", "9811111111", "1 year", "", "Bhopal", 7.5,
           "AI Developer Intern", "Python, RAG", "Contacted", datetime(2026, 4, 5), "", "", "", "", ""])
ws.append(["No Contact", "https://drive.example.com/y", "", "", "Fresher", "", "Bhopal", 7.0,
           "AI Developer Intern", "Python", "New", "", "", "", "", "", ""])
jd = wb.create_sheet("JDs")
jd.append(["Role", "Description"])
for r in ["AI Developer Intern", "AI Developer", "Digital Marketing Intern", "Sales Intern"]:
    jd.append([r, "..."])
wb.create_sheet("Config")
wb.save("sample_career_form.xlsx")
print("sample_career_form.xlsx written")
