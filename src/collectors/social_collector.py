"""
Social media profile collector for brand scoring.

Scrapes public social media profiles and extracts:
- Follower counts
- Post frequency
- Recent activity
- Engagement indicators

Supported platforms: Instagram, LinkedIn, TikTok, Twitter/X
"""

import re
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from src.api_key_pool import ApiKeySource, shared_api_key_pool
from src.config import FIRECRAWL_API_KEYS

@dataclass
class PlatformMetrics:
    """Metrics for a single social platform."""
    platform: str
    profile_url: str = ""
    followers_count: int = 0
    following_count: int = 0
    posts_count: int = 0
    last_post_date: str = ""
    avg_engagement_rate: float = 0.0
    verified: bool = False
    error: str = ""
    
    # Activity indicators
    posts_last_30_days: int = 0
    avg_likes: float = 0.0
    avg_comments: float = 0.0
    
    # Raw content for further analysis
    raw_content: str = ""


@dataclass
class SocialData:
    """Aggregated social media data for a brand."""
    brand_name: str
    platforms: Dict[str, PlatformMetrics] = field(default_factory=dict)
    profiles_found: List[str] = field(default_factory=list)
    total_followers: int = 0
    avg_post_frequency: float = 0.0  # posts per week
    most_active_platform: str = ""
    error: str = ""


class SocialCollector:
    """Collects social media data via Firecrawl CLI."""
    
    # Platform detection patterns
    PLATFORM_PATTERNS = {
        "instagram": {
            "domains": ["instagram.com"],
            "profile_pattern": r"instagram\.com/([a-zA-Z0-9._]+)/?",
            "exclude": ["p/", "explore/", "accounts/", "stories/"],
        },
        "twitter": {
            "domains": ["twitter.com", "x.com"],
            "profile_pattern": r"(?:twitter|x)\.com/([a-zA-Z0-9_]+)/?",
            "exclude": ["search", "login", "signup", "explore", "notifications", "messages", "i/"],
        },
        "linkedin": {
            "domains": ["linkedin.com"],
            "profile_pattern": r"linkedin\.com/(?:company|in)/([a-zA-Z0-9-]+)/?",
            "exclude": ["login", "signup", "jobs", "pulse", "feed/"],
        },
        "tiktok": {
            "domains": ["tiktok.com"],
            "profile_pattern": r"tiktok\.com/@([a-zA-Z0-9._]+)/?",
            "exclude": ["explore", "foryou", "login", "signup"],
        },
    }
    
    def __init__(self, api_key: ApiKeySource = None):
        """Initialize the social collector."""
        configured_keys = api_key if api_key is not None else FIRECRAWL_API_KEYS
        self._api_keys = shared_api_key_pool("firecrawl", configured_keys)
        self.api_key = api_key
    
    def _run_firecrawl(self, url: str) -> dict:
        """Scrape URL via Firecrawl Python SDK. Returns legacy {content, raw, error} shape."""
        if not self._api_keys:
            return {"error": "FIRECRAWL_API_KEY not set"}
        last_error = ""
        for attempt in range(self._api_keys.size):
            try:
                from firecrawl import Firecrawl

                doc = Firecrawl(api_key=self._api_keys.next_key()).scrape(
                    url, formats=["markdown"], timeout=60000
                )
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt + 1 < self._api_keys.size:
                    time.sleep(1.5)
        else:
            return {"error": last_error}
        content = (doc.markdown or "").strip()
        return {"content": content, "raw": content}
    
    def _detect_social_profiles_from_content(self, content: str) -> Dict[str, List[str]]:
        """
        Detect social media profile URLs from web content.
        Returns dict of platform -> list of profile URLs.
        """
        profiles = {platform: [] for platform in self.PLATFORM_PATTERNS}
        
        # Extract all URLs from content
        url_pattern = r'https?://(?:www\.)?([^\s<>"\']+?)(?:\s|$|<|"|\'|\)|\])'
        urls = re.findall(url_pattern, content.lower())
        
        for url in urls:
            for platform, config in self.PLATFORM_PATTERNS.items():
                # Check if URL matches platform domains
                if any(domain in url for domain in config["domains"]):
                    # Extract username using pattern
                    match = re.search(config["profile_pattern"], url)
                    if match:
                        username = match.group(1)
                        # Skip excluded paths
                        if not any(ex in url for ex in config["exclude"]):
                            # Reconstruct clean profile URL
                            if platform == "linkedin":
                                if "/company/" in url:
                                    profile_url = f"https://www.linkedin.com/company/{username}"
                                else:
                                    profile_url = f"https://www.linkedin.com/in/{username}"
                            elif platform == "twitter":
                                # Normalize to x.com
                                profile_url = f"https://x.com/{username}"
                            elif platform == "tiktok":
                                profile_url = f"https://www.tiktok.com/@{username}"
                            else:
                                profile_url = f"https://www.{platform}.com/{username}"
                            
                            if profile_url not in profiles[platform]:
                                profiles[platform].append(profile_url)
        
        return profiles
    
    def _search_social_profiles(self, brand_name: str) -> Dict[str, List[str]]:
        """
        Search for social profiles using common URL patterns.
        Returns dict of platform -> list of profile URLs.
        """
        profiles = {platform: [] for platform in self.PLATFORM_PATTERNS}
        
        # Generate common username variations
        brand_clean = re.sub(r'[^a-zA-Z0-9]', '', brand_name.lower())
        brand_username = brand_name.lower().replace(' ', '').replace('-', '')
        
        # Common username patterns
        usernames = list(set([
            brand_clean,
            brand_username,
            brand_name.lower().replace(' ', '_'),
            brand_name.lower().replace(' ', '.'),
            brand_name.lower().replace(' ', ''),
        ]))
        
        # Generate profile URLs to try
        for platform in self.PLATFORM_PATTERNS:
            for username in usernames[:2]:  # Limit to avoid too many requests
                if platform == "instagram":
                    profiles[platform].append(f"https://www.instagram.com/{username}")
                elif platform == "twitter":
                    profiles[platform].append(f"https://x.com/{username}")
                elif platform == "linkedin":
                    profiles[platform].append(f"https://www.linkedin.com/company/{username}")
                elif platform == "tiktok":
                    profiles[platform].append(f"https://www.tiktok.com/@{username}")
        
        return profiles
    
    def _extract_follower_count(self, content: str, platform: str) -> int:
        """Extract follower count from scraped content."""
        content_lower = content.lower()
        
        # Common patterns for follower counts
        patterns = [
            r'([\d,]+(?:\.\d+)?)\s*(?:k|m|b)?\s*followers',
            r'followers\s*[:=]\s*([\d,]+(?:\.\d+)?)\s*(?:k|m|b)?',
            r'([\d,]+(?:\.\d+)?)\s*(?:k|m|b)?\s*people\s*follow',
            r'([\d,]+(?:\.\d+)?)\s*(?:k|m|b)?\s*subscribers',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content_lower)
            if match:
                count_str = match.group(1).replace(',', '')
                multiplier = 1
                
                # Check for k, m, b suffixes
                suffix_match = re.search(r'(?:k|m|b)', content_lower[match.start():match.end()+2])
                if suffix_match:
                    suffix = suffix_match.group(0)
                    if suffix == 'k':
                        multiplier = 1000
                    elif suffix == 'm':
                        multiplier = 1000000
                    elif suffix == 'b':
                        multiplier = 1000000000
                
                try:
                    count = float(count_str) * multiplier
                    return int(count)
                except ValueError:
                    continue
        
        return 0
    
    def _extract_post_count(self, content: str, platform: str) -> int:
        """Extract post count from scraped content."""
        content_lower = content.lower()
        
        patterns = [
            r'(\d+(?:,\d+)?)\s*posts',
            r'posts\s*[:=]\s*(\d+(?:,\d+)?)',
            r'(\d+(?:,\d+)?)\s*tweets',
            r'tweets\s*[:=]\s*(\d+(?:,\d+)?)',
            r'(\d+(?:,\d+)?)\s*videos',
            r'videos\s*[:=]\s*(\d+(?:,\d+)?)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content_lower)
            if match:
                count_str = match.group(1).replace(',', '')
                try:
                    return int(count_str)
                except ValueError:
                    continue
        
        return 0
    
    def _extract_last_post_date(self, content: str) -> str:
        """Extract the date of the most recent post."""
        # Look for date patterns
        date_patterns = [
            r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})',
            r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})',
            r'(\d{4}-\d{2}-\d{2})',
            r'(\d{1,2}/\d{1,2}/\d{4})',
            r'(\d+\s+(?:hour|day|week|month)s?\s*ago)',
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                return match.group(1)
        
        return ""
    
    def _extract_engagement_metrics(self, content: str) -> Tuple[float, float]:
        """Extract average likes and comments from content."""
        content_lower = content.lower()
        
        # Look for like patterns
        likes_match = re.search(r'(?:avg|average|mean)\s*(?:likes?|hearts?)\s*[:=]\s*(\d+(?:\.\d+)?)', content_lower)
        likes = float(likes_match.group(1)) if likes_match else 0.0
        
        # Look for comment patterns
        comments_match = re.search(r'(?:avg|average|mean)\s*comments?\s*[:=]\s*(\d+(?:\.\d+)?)', content_lower)
        comments = float(comments_match.group(1)) if comments_match else 0.0
        
        return likes, comments
    
    def _check_verified(self, content: str) -> bool:
        """Check if the account is verified."""
        content_lower = content.lower()
        verified_signals = ['verified', '✓', 'checkmark', 'blue check', 'blue tick']
        return any(signal in content_lower for signal in verified_signals)
    
    def _scrape_profile(self, profile_url: str, platform: str) -> PlatformMetrics:
        """Scrape a single social media profile."""
        metrics = PlatformMetrics(platform=platform, profile_url=profile_url)
        
        # Scrape the profile page
        result = self._run_firecrawl(profile_url)
        
        if "error" in result:
            metrics.error = result["error"]
            return metrics
        
        content = result.get("content", "")
        metrics.raw_content = content
        
        # Extract metrics from content
        metrics.followers_count = self._extract_follower_count(content, platform)
        metrics.posts_count = self._extract_post_count(content, platform)
        metrics.last_post_date = self._extract_last_post_date(content)
        metrics.avg_likes, metrics.avg_comments = self._extract_engagement_metrics(content)
        metrics.verified = self._check_verified(content)
        
        # Calculate engagement rate if we have followers and likes
        if metrics.followers_count > 0 and metrics.avg_likes > 0:
            metrics.avg_engagement_rate = (metrics.avg_likes / metrics.followers_count) * 100
        
        return metrics
    
    def collect(self, brand_name: str, web_content: str = None) -> SocialData:
        """
        Collect social media data for a brand.
        
        Args:
            brand_name: Name of the brand
            web_content: Optional web content to search for social links
        
        Returns:
            SocialData with per-platform metrics
        """
        data = SocialData(brand_name=brand_name)
        
        # 1. Detect profiles from web content if provided
        all_profiles = {platform: [] for platform in self.PLATFORM_PATTERNS}
        
        if web_content:
            content_profiles = self._detect_social_profiles_from_content(web_content)
            for platform, urls in content_profiles.items():
                all_profiles[platform].extend(urls)
        
        # 2. Search for profiles using common patterns
        search_profiles = self._search_social_profiles(brand_name)
        for platform, urls in search_profiles.items():
            for url in urls:
                if url not in all_profiles[platform]:
                    all_profiles[platform].append(url)
        
        # 3. Scrape each detected profile
        for platform, profile_urls in all_profiles.items():
            if not profile_urls:
                continue
            
            # Try the first profile URL for each platform
            profile_url = profile_urls[0]
            data.profiles_found.append(profile_url)
            
            metrics = self._scrape_profile(profile_url, platform)
            data.platforms[platform] = metrics
            
            # Update aggregate metrics
            if metrics.followers_count > 0:
                data.total_followers += metrics.followers_count
        
        # 4. Calculate aggregate metrics
        active_platforms = [p for p, m in data.platforms.items() if m.posts_count > 0]
        if active_platforms:
            # Estimate post frequency (rough estimate based on post count)
            # Assuming average account age of 2 years
            total_posts = sum(data.platforms[p].posts_count for p in active_platforms)
            data.avg_post_frequency = total_posts / (52 * 2)  # posts per week
            
            # Find most active platform
            data.most_active_platform = max(
                active_platforms,
                key=lambda p: data.platforms[p].posts_count
            )
        
        return data
    
    def collect_from_urls(self, brand_name: str, profile_urls: Dict[str, str]) -> SocialData:
        """
        Collect social media data from known profile URLs.
        
        Args:
            brand_name: Name of the brand
            profile_urls: Dict mapping platform to profile URL
        
        Returns:
            SocialData with per-platform metrics
        """
        data = SocialData(brand_name=brand_name)
        
        for platform, profile_url in profile_urls.items():
            if platform not in self.PLATFORM_PATTERNS:
                continue
            
            data.profiles_found.append(profile_url)
            metrics = self._scrape_profile(profile_url, platform)
            data.platforms[platform] = metrics
            
            if metrics.followers_count > 0:
                data.total_followers += metrics.followers_count
        
        return data


# Convenience function
def collect_social_data(
    brand_name: str,
    web_content: str = None,
    api_key: str = None
) -> SocialData:
    """Collect social media data for a brand."""
    collector = SocialCollector(api_key=api_key)
    return collector.collect(brand_name, web_content)


# Example usage
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python -m src.collectors.social_collector <brand_name> [api_key]")
        sys.exit(1)
    
    brand = sys.argv[1]
    key = sys.argv[2] if len(sys.argv) > 2 else None
    
    data = collect_social_data(brand, api_key=key)
    
    print(f"\nSocial Data for: {data.brand_name}")
    print(f"Total Followers: {data.total_followers:,}")
    print(f"Avg Post Frequency: {data.avg_post_frequency:.1f} posts/week")
    print(f"Most Active Platform: {data.most_active_platform}")
    print(f"\nPlatforms Found: {len(data.platforms)}")
    
    for platform, metrics in data.platforms.items():
        print(f"\n[{platform.upper()}]")
        print(f"  URL: {metrics.profile_url}")
        print(f"  Followers: {metrics.followers_count:,}")
        print(f"  Posts: {metrics.posts_count:,}")
        print(f"  Last Post: {metrics.last_post_date}")
        print(f"  Verified: {metrics.verified}")
        if metrics.error:
            print(f"  Error: {metrics.error}")
