Retry an interrupted Unix unlock once so an errno-bearing signal exception cannot leave a phantom lock hold.
