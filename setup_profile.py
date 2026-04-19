"""
Profile Builder — Interactive CLI tool to understand your personality, skills, and preferences.
This sets up config/_profile.yml which is used by the evaluation engine.

Run: python setup_profile.py

These answers are critical for:
1. Positioning your CV correctly
2. Understanding what interviews will ask
3. Filtering jobs that match your personality
4. Building STAR stories for interviews
"""

import os
import json
import yaml
from pathlib import Path
from typing import Optional, List, Dict, Any

class ProfileBuilder:
    """Interactive profile questionnaire."""
    
    def __init__(self):
        self.profile_path = Path("config/_profile.yml")
        self.profile_path.parent.mkdir(exist_ok=True)
        self.responses = {}
        
    def ask(self, question: str, question_type: str = "text", options: Optional[List[str]] = None) -> Any:
        """Ask a question and get response."""
        print(f"\n❓ {question}")
        
        if question_type == "text":
            response = input("→ ").strip()
            if not response:
                print("  ⏭️  Skipping (empty response)")
                return None
            return response
            
        elif question_type == "multiline":
            print("  (Press ENTER twice when done)")
            lines = []
            while True:
                line = input("→ ").strip()
                if not line:
                    if lines:
                        break
                    print("  ⏭️  Skipping (empty response)")
                    return None
                lines.append(line)
            return "\n".join(lines)
            
        elif question_type == "multiple_choice":
            if not options:
                return None
            for i, opt in enumerate(options, 1):
                print(f"  {i}. {opt}")
            choice = input("→ Choose number: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(options):
                return options[int(choice) - 1]
            print("  ⏭️  Skipping (invalid choice)")
            return None
            
        elif question_type == "list":
            print("  (Enter each item, press ENTER twice when done)")
            items = []
            while True:
                item = input("→ ").strip()
                if not item:
                    if items:
                        break
                    print("  ⏭️  Skipping (empty list)")
                    return None
                items.append(item)
            return items
            
        return None
    
    def run(self):
        """Run the full questionnaire."""
        print("="*80)
        print("🎯 JOB-AGENT PROFILE BUILDER")
        print("="*80)
        print("\nThis questionnaire helps the AI understand your personality and goals.")
        print("Answer thoughtfully — these answers are used in interview preparation.\n")
        print("Skip any question by just pressing ENTER (leave blank).\n")
        
        # SECTION 1: WORK PERSONALITY
        print("\n" + "="*80)
        print("SECTION 1: YOUR WORK PERSONALITY")
        print("="*80)
        
        work_style = self.ask(
            "How would you describe your work style?",
            "multiple_choice",
            [
                "Systematic & methodical (plan everything first)",
                "Adaptive & flexible (adjust as I go)",
                "Collaborative & team-focused",
                "Independent & self-directed",
                "Mix of all above (depends on context)"
            ]
        )
        if work_style:
            self.responses["work_style"] = work_style
        
        pace = self.ask(
            "What pace do you prefer?",
            "multiple_choice",
            [
                "Fast-paced (rapid iteration, frequent changes)",
                "Deliberate (think before acting, stable environment)",
                "Mixed (fast feedback loops with solid planning)",
                "Depends on project"
            ]
        )
        if pace:
            self.responses["pace_preference"] = pace
        
        environment = self.ask(
            "Ideal team environment?",
            "text"
        )
        if environment:
            self.responses["team_environment"] = environment
        
        # SECTION 2: MOTIVATION & VALUES
        print("\n" + "="*80)
        print("SECTION 2: WHAT MOTIVATES YOU?")
        print("="*80)
        
        motivations = self.ask(
            "What motivates you most? (rank top 3, comma-separated: learning, stability, impact, money, autonomy, growth, respect, etc.)",
            "text"
        )
        if motivations:
            self.responses["top_motivations"] = [m.strip() for m in motivations.split(",")]
        
        values = self.ask(
            "What professional values matter most to you?",
            "multiline"
        )
        if values:
            self.responses["core_values"] = values
        
        impact_example = self.ask(
            "Describe a time when you felt most proud of your work (the impact/outcome matters, not just the task)",
            "multiline"
        )
        if impact_example:
            self.responses["pride_moment"] = impact_example
        
        # SECTION 3: EXPERTISE & PROOF POINTS
        print("\n" + "="*80)
        print("SECTION 3: YOUR UNIQUE STRENGTHS")
        print("="*80)
        
        superpower = self.ask(
            "What's your 'superpower'? What do people come to you for?",
            "text"
        )
        if superpower:
            self.responses["superpower"] = superpower
        
        unique_background = self.ask(
            "What makes your background unique compared to typical engineers?",
            "multiline"
        )
        if unique_background:
            self.responses["unique_background"] = unique_background
        
        proof_points = self.ask(
            "List your top 3-5 proof points (metrics, projects, achievements)",
            "list"
        )
        if proof_points:
            self.responses["proof_points"] = proof_points
        
        technical_strengths = self.ask(
            "Your top technical strengths (not just tools, but capabilities)",
            "text"
        )
        if technical_strengths:
            self.responses["technical_strengths"] = technical_strengths
        
        # SECTION 4: WHAT YOU AVOID / DEAL BREAKERS
        print("\n" + "="*80)
        print("SECTION 4: DEAL BREAKERS & AVOIDANCES")
        print("="*80)
        
        work_drains = self.ask(
            "What kind of work drains you or makes you unhappy?",
            "multiline"
        )
        if work_drains:
            self.responses["work_that_drains"] = work_drains
        
        deal_breakers = self.ask(
            "Hard deal-breakers (e.g., no on-site, no legacy monoliths, no startups <20 people)",
            "list"
        )
        if deal_breakers:
            self.responses["deal_breakers"] = deal_breakers
        
        skills_to_avoid = self.ask(
            "Technologies or domains you want to avoid?",
            "text"
        )
        if skills_to_avoid:
            self.responses["skills_to_avoid"] = skills_to_avoid
        
        # SECTION 5: CAREER GOALS
        print("\n" + "="*80)
        print("SECTION 5: CAREER GOALS & TRAJECTORY")
        print("="*80)
        
        next_2_years = self.ask(
            "What do you want to achieve in the next 2 years?",
            "multiline"
        )
        if next_2_years:
            self.responses["career_goal_2_years"] = next_2_years
        
        level_goal = self.ask(
            "What level are you targeting?",
            "multiple_choice",
            [
                "IC (Individual Contributor, stay hands-on)",
                "IC + people management (lead a small team)",
                "Manager (focus on team, less hands-on)",
                "Principal/Architect (deep expertise)",
                "Founder/Indie (own thing)"
            ]
        )
        if level_goal:
            self.responses["level_goal"] = level_goal
        
        learning_goals = self.ask(
            "What do you want to learn in the next role?",
            "multiline"
        )
        if learning_goals:
            self.responses["learning_goals"] = learning_goals
        
        # SECTION 6: INTERVIEW PREPARATION (STAR Stories)
        print("\n" + "="*80)
        print("SECTION 6: INTERVIEW STORIES (STAR Format)")
        print("="*80)
        print("\nThese are used to prepare for behavioral interview questions.")
        print("Tell stories, not just skills. Include: Situation, Task, Action, Result, Reflection\n")
        
        stories = []
        for i in range(1, 4):
            print(f"\n📖 Story #{i} (skip if empty)")
            story = self.ask(
                f"Tell a story about a challenge you overcame (keep it concrete)",
                "multiline"
            )
            if story:
                stories.append(story)
        
        if stories:
            self.responses["interview_stories"] = stories
        
        # SECTION 7: COMPENSATION & LOGISTICS
        print("\n" + "="*80)
        print("SECTION 7: COMPENSATION & LOGISTICS")
        print("="*80)
        
        salary_min = self.ask(
            "Minimum acceptable salary (annual, base only)?",
            "text"
        )
        if salary_min:
            self.responses["salary_min"] = salary_min
        
        salary_target = self.ask(
            "Target salary (what you'd be happy with)?",
            "text"
        )
        if salary_target:
            self.responses["salary_target"] = salary_target
        
        remote_requirement = self.ask(
            "Remote requirement?",
            "multiple_choice",
            [
                "Full remote (non-negotiable)",
                "Remote-first with occasional office (1-2 times/month)",
                "Hybrid (2-3 days/week office)",
                "Flexible (open to discussion)"
            ]
        )
        if remote_requirement:
            self.responses["remote_requirement"] = remote_requirement
        
        relocation = self.ask(
            "Open to relocation?",
            "multiple_choice",
            ["No", "Yes, within [country]", "Yes, internationally (with conditions)", "Only if [location]"]
        )
        if relocation:
            self.responses["relocation"] = relocation
        
        # SECTION 8: PERSONALITY TRAITS
        print("\n" + "="*80)
        print("SECTION 8: YOUR PERSONALITY AT WORK")
        print("="*80)
        
        strengths = self.ask(
            "Top 3 personality strengths (e.g., detail-oriented, communicator, quick learner, creative)",
            "text"
        )
        if strengths:
            self.responses["personality_strengths"] = strengths
        
        growth_areas = self.ask(
            "Areas you're working on (e.g., public speaking, delegation, asking for help)",
            "text"
        )
        if growth_areas:
            self.responses["growth_areas"] = growth_areas
        
        conflict_style = self.ask(
            "How do you handle conflict?",
            "multiline"
        )
        if conflict_style:
            self.responses["conflict_resolution"] = conflict_style
        
        # SECTION 9: NARRATIVE (for outreach)
        print("\n" + "="*80)
        print("SECTION 9: YOUR PROFESSIONAL NARRATIVE")
        print("="*80)
        
        elevator_pitch = self.ask(
            "1-sentence elevator pitch (who you are + what you do + what you want)",
            "text"
        )
        if elevator_pitch:
            self.responses["elevator_pitch"] = elevator_pitch
        
        career_arc = self.ask(
            "Briefly describe your career journey so far (why each move, what you learned)",
            "multiline"
        )
        if career_arc:
            self.responses["career_arc"] = career_arc
        
        # Save profile
        print("\n" + "="*80)
        print("✅ PROFILE CREATED")
        print("="*80)
        
        self._save_profile()
        print(f"\n✓ Profile saved to: {self.profile_path.absolute()}")
        print(f"✓ Responses recorded: {len(self.responses)} fields")
        print("\nYour profile is now ready for the job evaluation engine.")
        
    def _save_profile(self):
        """Save responses to YAML."""
        with open(self.profile_path, 'w') as f:
            yaml.dump(
                self.responses,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False
            )


if __name__ == "__main__":
    builder = ProfileBuilder()
    builder.run()
