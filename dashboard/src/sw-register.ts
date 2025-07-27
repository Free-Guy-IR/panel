// Service Worker Registration
export function registerSW() {
  if ('serviceWorker' in navigator) {
    // Get the base URL from Vite's environment
    const baseUrl = import.meta.env.BASE_URL || '/'
    
    // Register service worker
    navigator.serviceWorker.register(`${baseUrl}service-worker.js`)
      .then((registration) => {
        console.log('SW registered: ', registration)
        
        registration.addEventListener('updatefound', () => {
          const newWorker = registration.installing
          if (newWorker) {
            newWorker.addEventListener('statechange', () => {
              if (newWorker.state === 'installed' && navigator.serviceWorker.controller) {
                console.log('New content is available')
              }
            })
          }
        })
      })
      .catch((registrationError) => {
        console.log('SW registration failed: ', registrationError)
      })
  }
} 