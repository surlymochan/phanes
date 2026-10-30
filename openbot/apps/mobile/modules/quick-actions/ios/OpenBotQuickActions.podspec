Pod::Spec.new do |s|
  s.name           = 'OpenBotQuickActions'
  s.version        = '1.0.0'
  s.summary        = 'Home Screen quick actions for OpenBot'
  s.description    = 'Sets the quick actions of the app icon and gives each chosen action to JavaScript.'
  s.license        = 'PolyForm-Noncommercial-1.0.0'
  s.author         = 'OpenBot'
  s.homepage       = 'https://openbot.run'
  s.platforms      = { :ios => '16.4' }
  s.swift_version  = '5.9'
  s.source         = { git: '' }
  s.static_framework = true

  s.dependency 'ExpoModulesCore'

  s.source_files = "**/*.swift"
  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES',
    'SWIFT_COMPILATION_MODE' => 'wholemodule'
  }
end
