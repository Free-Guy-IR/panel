import i18n from 'i18next'
import LanguageDetector from 'i18next-browser-languagedetector'
import HttpApi from 'i18next-http-backend'

/** Replaced at build time; falls back to a constant during development. */
const BUILD_ID = import.meta.env.VITE_BUILD_ID || 'dev'
import { initReactI18next } from 'react-i18next'
import { joinURL } from 'ufo'

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .use(HttpApi)
  .init(
    {
      debug: import.meta.env.NODE_ENV === 'development',
      returnNull: false,
      fallbackLng: 'en',
      interpolation: {
        escapeValue: false,
      },
      react: {
        useSuspense: true,
      },
      load: 'languageOnly',
      detection: {
        caches: ['localStorage'],
      },
      backend: {
        // Tied to the build, so a new one is fetched exactly once per deploy
        // rather than the browser holding on to the previous translations.
        loadPath: joinURL(import.meta.env.BASE_URL, `statics/locales/{{lng}}.json?v=${BUILD_ID}`),
      },
    },
    function (err) {
      if (err) {
        console.error('i18next initialization error:', err)
      }
      const lang = i18n.language
      document.documentElement.lang = lang
      document.documentElement.setAttribute('dir', i18n.dir())
    },
  )

export default i18n
