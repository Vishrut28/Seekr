"""Seed a synthetic corpus into rip.db so the benchmark reflects a realistic
data volume instead of an empty database. Local only, no network calls.
"""
import random
import sys

sys.path.insert(0, ".")

from rip.db import Base, engine, SessionLocal
from rip.ingest import ingest_profile
from rip.normalize import EvidenceItem, NormalizedProfile, OrgAffiliation, ProjectData, PublicationData

random.seed(42)

FIRST = ["Aarav", "Priya", "Rohan", "Ananya", "Vikram", "Neha", "Arjun", "Divya",
         "Karan", "Sneha", "Rahul", "Meera", "Aditya", "Kavya", "Ishaan", "Riya",
         "Aiden", "Emma", "Liam", "Olivia", "Noah", "Sophia", "Lucas", "Mia",
         "Wei", "Yuki", "Hiro", "Sara", "Omar", "Fatima", "Chen", "Ling"]
LAST = ["Sharma", "Patel", "Kumar", "Singh", "Reddy", "Nair", "Gupta", "Iyer",
        "Rao", "Verma", "Chen", "Wang", "Kim", "Park", "Smith", "Johnson",
        "Mueller", "Silva", "Kato", "Suzuki", "Cole", "Bennett", "Fischer"]
SKILLS = ["Distributed Systems", "Machine Learning", "Computer Vision", "Robotics",
          "Reinforcement Learning", "Natural Language Processing", "Rust",
          "Kubernetes", "Compiler Optimization", "High-Performance Computing",
          "Cybersecurity", "Vulnerability Research", "Graph Neural Networks",
          "Model Training Infrastructure", "Multimodal AI", "Backend Engineering",
          "Product Design", "AI Infrastructure", "Data Engineering", "Databases",
          "Privacy-Preserving Machine Learning", "LLM Evaluation"]
ORGS = ["Google", "Microsoft", "OpenAI", "Deccan AI", "Meta", "Amazon", "DeepMind",
        "IIT Bombay", "Stanford University", "Zeta Corp", "Acme Labs"]
LOCATIONS = ["Bangalore, India", "Mumbai, India", "Hyderabad, India", "Berlin, Germany",
             "London, United Kingdom", "San Francisco, California", "Toronto, Canada",
             "Singapore", "Pune, India", "Delhi, India"]
ROLES = ["Software Engineer", "Research Scientist", "Product Designer",
         "Data Engineer", "ML Engineer", "Principal Engineer", "Robotics Engineer"]


def rand_person(i: int) -> NormalizedProfile:
    name = f"{random.choice(FIRST)} {random.choice(LAST)}"
    orgs = random.sample(ORGS, k=random.randint(1, 2))
    skills = random.sample(SKILLS, k=random.randint(1, 3))
    country = "IN" if "India" in LOCATIONS[0] and random.random() < 0.4 else None
    return NormalizedProfile(
        source="github", source_type="code_hosting",
        external_id=f"bench{i}", url=f"https://github.com/bench{i}",
        raw={"login": f"bench{i}"}, name=name, usernames=[f"github:bench{i}"],
        location=random.choice(LOCATIONS), country=country,
        organizations=[
            OrgAffiliation(name=o, role=random.choice(ROLES), is_current=(idx == 0))
            for idx, o in enumerate(orgs)
        ],
        evidence=[
            EvidenceItem(attribute_type="skill", value=s, confidence=round(random.uniform(0.5, 0.95), 2))
            for s in skills
        ],
        projects=[ProjectData(
            name=f"{name.split()[0].lower()}-proj{i}", url=f"https://github.com/bench{i}/proj",
            technologies=[random.choice(skills)],
            activity={"stars": random.randint(0, 4000), "forks": random.randint(0, 200)},
            last_active_at=f"202{random.randint(2, 6)}-0{random.randint(1, 9)}-01",
        )] if random.random() < 0.5 else [],
        publications=[PublicationData(
            title=f"A study on {random.choice(skills)}", external_id=f"W{i}",
            citations=random.randint(0, 800), published_date=f"202{random.randint(1, 6)}",
            topics=[random.choice(skills)],
        )] if random.random() < 0.3 else [],
    )


def main(n: int = 800) -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        for i in range(n):
            ingest_profile(session, rand_person(i))
            if i % 100 == 0:
                session.commit()
                print(f"  ...{i}/{n}")
        session.commit()
    print(f"seeded {n} people into rip.db")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 800)
