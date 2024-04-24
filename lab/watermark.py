import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import numpy as np

# Create a 6x4 array with random colors
border = [255, 255, 255]
white = [240, 240, 240]
eye = [96, 51, 48]
mouse = [252, 197, 105]
face = [251, 195, 194]
array = np.array([[border, white, white, white, white, border], 
                  [white, eye, white, white, eye, white], 
                  [face, white, mouse, mouse, white, face], 
                  [border, white, white, white, white, border]])

# Visualize the array
fig, ax = plt.subplots()
ax.imshow(array, interpolation='nearest')
plt.axis('off')  # Turn off axis numbers and ticks
plt.show()