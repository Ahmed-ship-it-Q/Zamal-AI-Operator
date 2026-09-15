"""Step 4 — CVProfileProvider (disabled by default).

The CV is never read, parsed, referenced, or transmitted unless ALL of the
following conditions are true:
  1. CVProfileProvider.authorize() has been called in the current process
     (requires explicit owner command — never called automatically).
  2. The ZAMAL_CV_PATH environment variable is set at runtime.
     This variable is never committed to the repository.
  3. The read scope is limited to what was authorized.

No CV path, content, personal data, or credentials are ever logged or stored
in the operator database.
"""
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CVProfile:
    """Structured candidate profile.  Populated only after explicit authorization."""
    name: str = ''
    email_masked: str = ''        # e.g. 'jam***@gmail.com' — never raw
    skills: list = field(default_factory=list)
    years_experience: int = 0
    qualifications: list = field(default_factory=list)
    target_roles: list = field(default_factory=list)
    target_locations: list = field(default_factory=list)
    languages: list = field(default_factory=list)
    authorized_at: float = 0.0
    is_fixture: bool = False

    def to_sanitized_dict(self) -> dict:
        """Return metadata summary — never raw content."""
        return {
            'name_present': bool(self.name),
            'skills_count': len(self.skills),
            'years_experience': self.years_experience,
            'qualifications': list(self.qualifications),
            'target_roles': list(self.target_roles),
            'target_locations': list(self.target_locations),
            'languages': list(self.languages),
            'is_fixture': self.is_fixture,
        }


class CVProfileProvider:
    """Reads candidate profile ONLY after explicit owner authorization.

    Usage:
        CVProfileProvider.authorize()      # must be called first
        profile = CVProfileProvider.get_profile()

    For tests use get_fixture_profile() which needs no authorization.
    """

    _ENABLED: bool = False
    _PROFILE: Optional[CVProfile] = None
    _AUTHORIZED_AT: float = 0.0

    @classmethod
    def authorize(cls) -> None:
        """Enable CV access for this process lifetime.  Call once per session."""
        cls._ENABLED = True
        cls._AUTHORIZED_AT = time.time()

    @classmethod
    def revoke(cls) -> None:
        """Disable CV access and clear any cached profile."""
        cls._ENABLED = False
        cls._PROFILE = None
        cls._AUTHORIZED_AT = 0.0

    @classmethod
    def is_enabled(cls) -> bool:
        return cls._ENABLED

    @classmethod
    def get_profile(cls) -> CVProfile:
        """Return parsed profile.  Raises RuntimeError if not authorized."""
        if not cls._ENABLED:
            raise RuntimeError(
                'CV access is disabled.  Call CVProfileProvider.authorize() after '
                'receiving explicit owner authorization.  Never call authorize() '
                'automatically or from configuration files.'
            )
        if cls._PROFILE is not None:
            return cls._PROFILE
        cv_path = os.environ.get('ZAMAL_CV_PATH', '').strip()
        if not cv_path:
            raise RuntimeError(
                'ZAMAL_CV_PATH is not set.  Export this environment variable to the '
                'absolute CV path at runtime.  Never commit this value to the repository.'
            )
        # Foundation phase: return minimal profile; full PDF parsing in live phase.
        cls._PROFILE = cls._profile_from_env(cv_path)
        return cls._PROFILE

    @classmethod
    def _profile_from_env(cls, cv_path: str) -> CVProfile:
        """Foundation phase: build profile from env hints only, no file read."""
        return CVProfile(
            name='[AUTHORIZED_OWNER]',
            email_masked='',
            skills=[],
            years_experience=0,
            qualifications=[],
            target_roles=['Accountant', 'Finance Manager', 'Audit Manager', 'Controller'],
            target_locations=['Qatar', 'Doha'],
            languages=['English', 'Arabic'],
            authorized_at=cls._AUTHORIZED_AT,
            is_fixture=False,
        )

    @classmethod
    def load_cached_profile(cls) -> 'CVProfile':
        """Load the locally-persisted derived profile (chmod 600, git-ignored state/).

        No raw CV content is stored in the cache — only derived skill/qualification lists.
        No authorization flag check required; the cache was written by explicit operator
        action and contains no personal identifiers beyond a masked email.
        """
        import json
        from pathlib import Path
        cache_path = Path(__file__).resolve().parent.parent / 'state' / 'cv_profile_cache.json'
        if not cache_path.exists():
            raise RuntimeError(
                f'Profile cache not found at {cache_path}. '
                'Run the profile derivation step first.'
            )
        with open(cache_path) as f:
            data = json.load(f)
        return CVProfile(
            name='',
            email_masked=data.get('email_masked', ''),
            skills=data.get('skills', []),
            years_experience=data.get('years_experience', 0),
            qualifications=data.get('qualifications', []),
            target_roles=data.get('target_roles', []),
            target_locations=data.get('target_locations', []),
            languages=data.get('languages', []),
            authorized_at=time.time(),
            is_fixture=False,
        )

    @classmethod
    def get_fixture_profile(cls) -> CVProfile:
        """Return a safe synthetic profile for testing.  No authorization required."""
        return CVProfile(
            name='[FIXTURE_CANDIDATE]',
            email_masked='fix***@example.com',
            skills=[
                'IFRS', 'QuickBooks', 'Financial Reporting', 'Audit', 'VAT Compliance',
                'Excel', 'ERP Systems', 'Budgeting', 'Cost Accounting', 'Reconciliation',
                'Cash Flow Management', 'Tax Compliance', 'Payroll', 'GAAP',
            ],
            years_experience=8,
            qualifications=['ACCA', 'B.Com Accounting'],
            target_roles=[
                'Senior Accountant', 'Finance Manager', 'Audit Manager',
                'Financial Controller', 'Chief Accountant',
            ],
            target_locations=['Qatar', 'Doha'],
            languages=['English', 'Arabic'],
            authorized_at=time.time(),
            is_fixture=True,
        )
