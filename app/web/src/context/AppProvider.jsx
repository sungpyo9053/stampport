import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  clearUser,
  loadProfileMeta,
  loadStamps,
  loadUser,
  makeNewProfile,
  saveProfileMeta,
  saveStamps,
  saveUser,
} from '../utils/storage.js';
import { computeBadges, BADGE_DEFS } from '../data/badges.js';
import { computeQuests } from '../data/quests.js';
import { expGainFor, levelFor, levelProgress, totalExp } from '../utils/leveling.js';
import { generateKickPoints } from '../utils/kickPoints.js';
import { AppContext } from './appContext.js';

function makeStampId() {
  return `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export function AppProvider({ children }) {
  // loadUser() already migrates legacy {nickname, email} blobs into
  // the new passport-identity shape, so the rest of the provider can
  // just trust the schema.
  const [user, setUser] = useState(() => loadUser());
  const [stamps, setStamps] = useState(() => {
    const u = loadUser();
    return u ? loadStamps(u.user_id) : [];
  });
  const [profileMeta, setProfileMeta] = useState(() => {
    const u = loadUser();
    return u ? loadProfileMeta(u.user_id) : { selected_title_id: null };
  });

  useEffect(() => {
    if (user) saveUser(user);
  }, [user]);

  useEffect(() => {
    if (user) saveStamps(user.user_id, stamps);
  }, [user, stamps]);

  useEffect(() => {
    if (user) saveProfileMeta(user.user_id, profileMeta);
  }, [user, profileMeta]);

  // Generic login entry point — used by both the legacy guest form
  // (nickname/email) and the new social/mock flows. Caller passes
  // provider + nickname; everything else is filled in by
  // makeNewProfile and merged on top of any existing profile so a
  // returning user keeps their stamps.
  const loginAs = useCallback(
    ({
      provider = 'guest',
      nickname,
      email = null,
      provider_user_id = null,
      avatar_style,
      passport_title,
    } = {}) => {
      const existing = loadUser();
      let nextUser;
      // Returning user with the same provider + provider_user_id (or
      // same email for guest) — keep their existing user_id so the
      // stamps bucket is preserved.
      const isSameUser = !!existing
        && existing.provider === provider
        && (provider_user_id
          ? existing.provider_user_id === provider_user_id
          : email
          ? existing.email === email
          : provider === 'guest');
      if (isSameUser) {
        nextUser = {
          ...existing,
          nickname: nickname || existing.nickname,
          provider_user_id: provider_user_id ?? existing.provider_user_id,
          email: email ?? existing.email,
          avatar_style: avatar_style || existing.avatar_style,
          passport_title: passport_title || existing.passport_title,
          last_login_at: new Date().toISOString(),
        };
      } else {
        nextUser = makeNewProfile({
          provider,
          nickname,
          email,
          provider_user_id,
          avatar_style,
          passport_title,
        });
      }
      setUser(nextUser);
      setStamps(loadStamps(nextUser.user_id));
      setProfileMeta(loadProfileMeta(nextUser.user_id));
      saveUser(nextUser);
      return nextUser;
    },
    [],
  );

  // Back-compat: the legacy nickname+email login form still calls
  // `login({ nickname, email })`. Forward to loginAs as a guest so
  // the existing screen keeps working without a rewrite.
  const login = useCallback(
    ({ nickname, email }) => loginAs({ provider: 'guest', nickname, email }),
    [loginAs],
  );

  const logout = useCallback(() => {
    clearUser();
    setUser(null);
    setStamps([]);
    setProfileMeta({ selected_title_id: null });
  }, []);

  const addStamp = useCallback(
    (input) => {
      const previous = stamps;
      const visited_at = input.visited_at || new Date().toISOString().slice(0, 10);
      const stamp = {
        id: makeStampId(),
        user_id: user?.user_id,
        place_name: input.place_name?.trim() || '',
        area: input.area || '기타',
        category: input.category || 'cafe',
        tags: input.tags || [],
        representative_menu: input.representative_menu?.trim() || '',
        visited_at,
        verification_level: input.verification_level || 'manual',
        verification_status: 'unverified',
        trust_score: 0,
        created_at: new Date().toISOString(),
      };
      stamp.kick_points = generateKickPoints(stamp);
      const exp_gained = expGainFor(stamp, previous);
      stamp.exp_gained = exp_gained;
      setStamps([stamp, ...previous]);
      return stamp;
    },
    [stamps, user],
  );

  const setSelectedTitle = useCallback((badgeId) => {
    setProfileMeta((prev) => ({ ...prev, selected_title_id: badgeId }));
  }, []);

  const exp = useMemo(() => totalExp(stamps), [stamps]);
  const level = useMemo(() => levelFor(exp), [exp]);
  const levelInfo = useMemo(() => levelProgress(exp), [exp]);
  const badges = useMemo(() => computeBadges(stamps), [stamps]);
  const quests = useMemo(() => computeQuests(stamps), [stamps]);

  const earnedBadges = useMemo(() => badges.filter((b) => b.earned), [badges]);

  const selectedTitle = useMemo(() => {
    const id = profileMeta.selected_title_id;
    if (id) {
      const def = BADGE_DEFS.find((d) => d.id === id);
      const earned = earnedBadges.find((b) => b.id === id);
      if (def && earned) return def.titleLabel;
    }
    if (earnedBadges.length > 0) {
      const def = BADGE_DEFS.find((d) => d.id === earnedBadges[earnedBadges.length - 1].id);
      return def?.titleLabel || '로컬 미식 새내기';
    }
    return '로컬 미식 새내기';
  }, [profileMeta, earnedBadges]);

  const stampById = useCallback(
    (id) => stamps.find((s) => s.id === id) || null,
    [stamps],
  );

  const value = useMemo(
    () => ({
      user,
      stamps,
      addStamp,
      login,
      loginAs,
      logout,
      exp,
      level,
      levelInfo,
      badges,
      earnedBadges,
      quests,
      selectedTitle,
      profileMeta,
      setSelectedTitle,
      stampById,
    }),
    [
      user,
      stamps,
      addStamp,
      login,
      loginAs,
      logout,
      exp,
      level,
      levelInfo,
      badges,
      earnedBadges,
      quests,
      selectedTitle,
      profileMeta,
      setSelectedTitle,
      stampById,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
