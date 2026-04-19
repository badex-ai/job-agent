"""
Profile Manager — Load and manage the user profile created by setup_profile.py
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List


class ProfileManager:
    """Load and access user profile."""
    
    def __init__(self, profile_path: str = "config/_profile.yml"):
        self.profile_path = Path(profile_path)
        self.profile = self._load_profile()
    
    def _load_profile(self) -> Dict[str, Any]:
        """Load profile from YAML."""
        if not self.profile_path.exists():
            return {}
        
        with open(self.profile_path, 'r') as f:
            return yaml.safe_load(f) or {}
    
    def is_empty(self) -> bool:
        """Check if profile has any data."""
        return len(self.profile) == 0
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a profile field."""
        return self.profile.get(key, default)
    
    def get_summary(self) -> str:
        """Get a human-readable summary of the profile."""
        if self.is_empty():
            return "❌ Profile not set. Run: python setup_profile.py"
        
        lines = []
        lines.append("\n📋 YOUR PROFILE SUMMARY")
        lines.append("="*60)
        
        # Work personality
        if "work_style" in self.profile:
            lines.append(f"🎯 Work Style: {self.profile['work_style']}")
        if "pace_preference" in self.profile:
            lines.append(f"⚡ Pace: {self.profile['pace_preference']}")
        
        # Motivations
        if "top_motivations" in self.profile:
            lines.append(f"💪 Motivations: {', '.join(self.profile['top_motivations'])}")
        
        # Superpower
        if "superpower" in self.profile:
            lines.append(f"✨ Superpower: {self.profile['superpower']}")
        
        # Deal breakers
        if "deal_breakers" in self.profile:
            lines.append(f"🚫 Deal Breakers: {', '.join(self.profile['deal_breakers'])}")
        
        # Career goal
        if "career_goal_2_years" in self.profile:
            lines.append(f"🎯 2-Year Goal: {self.profile['career_goal_2_years'][:100]}...")
        
        # Compensation
        if "salary_target" in self.profile:
            lines.append(f"💰 Target Salary: {self.profile['salary_target']}")
        
        # Remote
        if "remote_requirement" in self.profile:
            lines.append(f"🌍 Remote: {self.profile['remote_requirement']}")
        
        lines.append("="*60)
        return "\n".join(lines)
    
    def get_evaluation_context(self) -> str:
        """Get profile data formatted for CV matching prompt."""
        if self.is_empty():
            return ""
        
        context = []
        context.append("\n## USER PROFILE (from setup_profile.py)\n")
        
        # Personality
        if "work_style" in self.profile:
            context.append(f"**Work Style:** {self.profile['work_style']}")
        if "pace_preference" in self.profile:
            context.append(f"**Pace Preference:** {self.profile['pace_preference']}")
        
        # Motivations
        if "top_motivations" in self.profile:
            mots = ", ".join(self.profile['top_motivations'])
            context.append(f"**Top Motivations:** {mots}")
        
        # Superpower
        if "superpower" in self.profile:
            context.append(f"**Superpower:** {self.profile['superpower']}")
        
        # Unique background
        if "unique_background" in self.profile:
            context.append(f"**Unique Background:** {self.profile['unique_background']}")
        
        # What drains them
        if "work_that_drains" in self.profile:
            context.append(f"**Avoidances:** {self.profile['work_that_drains']}")
        
        # Career goal
        if "career_goal_2_years" in self.profile:
            context.append(f"**Career Goal (2 years):** {self.profile['career_goal_2_years']}")
        
        # Deal breakers
        if "deal_breakers" in self.profile:
            breakers = ", ".join(self.profile['deal_breakers'])
            context.append(f"**Deal Breakers:** {breakers}")
        
        # Level goal
        if "level_goal" in self.profile:
            context.append(f"**Level Goal:** {self.profile['level_goal']}")
        
        # Compensation
        if "salary_min" in self.profile or "salary_target" in self.profile:
            min_sal = self.profile.get("salary_min", "N/A")
            target_sal = self.profile.get("salary_target", "N/A")
            context.append(f"**Salary Range:** ${min_sal} (min) - ${target_sal} (target)")
        
        # Remote requirement
        if "remote_requirement" in self.profile:
            context.append(f"**Remote Requirement:** {self.profile['remote_requirement']}")
        
        return "\n".join(context)
    
    def get_interview_stories(self) -> List[str]:
        """Get interview stories."""
        return self.profile.get("interview_stories", [])
    
    def get_deal_breakers(self) -> List[str]:
        """Get deal breakers for quick filtering."""
        return self.profile.get("deal_breakers", [])
    
    def get_skills_to_avoid(self) -> List[str]:
        """Get skills to avoid."""
        skill_text = self.profile.get("skills_to_avoid", "")
        if not skill_text:
            return []
        return [s.strip().lower() for s in skill_text.split(",")]


def ensure_profile_exists():
    """Check if profile exists, prompt user to create it if not."""
    profile_path = Path("config/_profile.yml")
    
    if not profile_path.exists():
        print("\n" + "="*80)
        print("⚠️  PROFILE NOT FOUND")
        print("="*80)
        print("\nBefore running the job agent, you need to set up your profile.")
        print("This helps the AI understand your personality and preferences.")
        print("\nRun this command to create your profile:")
        print("  python setup_profile.py")
        print("\nThe questionnaire takes ~10-15 minutes to complete.")
        print("="*80 + "\n")
        return False
    
    return True
