// Paste your Firebase configuration here
const firebaseConfig = {
    apiKey: "AIzaSyAmLanEMQoY6NSEcacziav2xYyr4y8UOHU",
    authDomain: "g-trackapp.firebaseapp.com",
    databaseURL: "https://g-trackapp-default-rtdb.asia-southeast1.firebasedatabase.app",
    projectId: "g-trackapp",
    storageBucket: "g-trackapp.firebasestorage.app",
    messagingSenderId: "172056621672",
    appId: "1:172056621672:web:9481cda9b93cac6d7d01dd",
    measurementId: "G-5HRRQCP4P1"
};

// Initialize Firebase with your configuration
const app = firebase.initializeApp(firebaseConfig);
const db = firebase.firestore();
const auth = firebase.auth();
const storage = firebase.storage();

// A function to fetch and display the user's profile data
function fetchUserProfile(user) {
    if (user) {
        // Use the user's unique UID to get their document
        const userDocRef = db.collection("admin").doc(user.uid); 

        userDocRef.get().then((doc) => {
            if (doc.exists) {
                // Get the data from the document
                const data = doc.data();

                // Populate the HTML fields with the retrieved data
                document.getElementById('username-display').textContent = data.username || 'N/A';
                document.getElementById('firstname-input').value = data.firstname || '';
                document.getElementById('lastname-input').value = data.lastname || '';
                document.getElementById('contact-input').value = data.contact || '';
                document.getElementById('email-input').value = data.email || '';
                
                if (data.photoURL) {
                    document.getElementById('profile-preview').src = data.photoURL;
                }
            } else {
                console.log("No user profile found!");
            }
        }).catch((error) => {
            console.error("Error getting user document:", error);
        });
    }
}

// Function to handle saving the user profile
function saveProfile() {
    const user = auth.currentUser;
    if (user) {
        // Use the user's unique UID to update their document
        const userRef = db.collection("admin").doc(user.uid); 

        const updates = {
            firstname: document.getElementById('firstname-input').value,
            lastname: document.getElementById('lastname-input').value,
            contact: document.getElementById('contact-input').value,
            email: document.getElementById('email-input').value
        };

        userRef.update(updates)
            .then(() => {
                console.log("Profile successfully updated!");
                alert("Profile saved successfully!");
            })
            .catch((error) => {
                console.error("Error updating profile: ", error);
                alert("Error saving profile. Please try again.");
            });
    } else {
        alert("No user is signed in.");
    }
}

// Handle profile photo upload
document.getElementById('photo-input').addEventListener('change', async (event) => {
    const file = event.target.files[0];
    const user = auth.currentUser;
    if (!file || !user) return;

    try {
        const storageRef = storage.ref(`profile_pictures/${user.uid}`);
        const uploadTask = storageRef.put(file);

        uploadTask.on('state_changed',
            (snapshot) => {
                // You can add a progress bar here
            },
            (error) => {
                console.error("Upload error:", error);
            },
            async () => {
                const downloadURL = await uploadTask.snapshot.ref.getDownloadURL();
                // Use the user's unique UID to update their profile photo
                const userDocRef = db.collection('admin').doc(user.uid);
                await userDocRef.update({
                    photoURL: downloadURL
                });
                document.getElementById('profile-preview').src = downloadURL;
                alert('Profile photo updated successfully!');
            }
        );
    } catch (error) {
        alert('Error uploading photo: ' + error.message);
    }
});

// Update the form submission event listener to call the saveProfile function
document.getElementById('profile-form').addEventListener('submit', function(event) {
    event.preventDefault(); 
    saveProfile();
});

// Use onAuthStateChanged to ensure the user is logged in before fetching data
auth.onAuthStateChanged(function(user) {
    if (user) {
        fetchUserProfile(user);
    } else {
        console.log("No user is signed in.");
    }
});